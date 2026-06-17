"""CiviCRM API4 client for door-sync.

Reads contacts with a non-empty card_id from a WordPress-hosted CiviCRM
instance, augmented with their active (Current/Grace) membership type
labels. Contacts with a card_id but no active membership are still returned
with membership_types=[]; they resolve to "unmapped" downstream and the
safety guard halts on them — surfacing the data issue rather than silently
ignoring a provisioned card.

Read-only — no write operations. Hand-rolled retry on 5xx and 429.

This module is not pure (it does HTTP), but it does NOT call sys.exit.
Errors surface as CivicrmClientError so the scheduler's per-cycle try/except
can log and continue.
"""

import json
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

import httpx

from door_sync.config import CivicrmConfig
from door_sync.models import CiviMember

_API_PATH = "/civicrm/ajax/api4"
_PAGE_SIZE = 250
_CONTACT_BATCH_SIZE = (
    500  # Caps contact_ids per Membership.get IN clause to keep request body bounded
)
_DEFAULT_ACTIVE_STATUSES = ("Current", "Grace", "New")
_MAX_PAGES = 1_000  # 250,000 records — far above any plausible deployment
_MAX_ATTEMPTS = 3


class CivicrmClientError(Exception):
    """Raised on non-recoverable CiviCRM API failure."""


class CivicrmClient:
    """Read-only CiviCRM API4 client.

    Construct one per reconcile cycle; the underlying httpx.Client owns
    connection state. Use as a context manager, or call close() explicitly.
    """

    def __init__(self, config: CivicrmConfig) -> None:
        """Initialize the CiviCRM API4 client.

        Args:
            config: CiviCRM connection settings including host, API key, and card ID field.
        """
        self._config = config
        self._http = httpx.Client(
            base_url=config.host,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=True,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def fetch_active(self) -> list[CiviMember]:
        """Fetch all contacts with a card ID and their active membership types.

        Returns:
            List of `CiviMember` records. Contacts with no active membership
            are included with an empty `membership_types` tuple.

        Raises:
            CivicrmClientError: On API failure, malformed response, or unparseable card ID.
        """
        contacts = self._fetch_contacts()
        if not contacts:
            return []
        contact_ids = [int(c["id"]) for c in contacts]
        memberships = self._fetch_memberships(contact_ids)

        types_by_contact: dict[int, list[str]] = {}
        for m in memberships:
            cid = int(m["contact_id"])
            label = str(m["membership_type_id:label"])
            types_by_contact.setdefault(cid, []).append(label)

        result: list[CiviMember] = []
        for c in contacts:
            cid = int(c["id"])
            raw_card_id = c.get(self._config.card_id_field)
            try:
                card_id = _coerce_card_id(raw_card_id)
            except (ValueError, TypeError) as e:
                redacted = (
                    "non-numeric"
                    if isinstance(raw_card_id, str)
                    else f"type {type(raw_card_id).__name__}"
                )
                raise CivicrmClientError(
                    f"contact {cid}: card_id field "
                    f"{self._config.card_id_field!r} has unparseable value "
                    f"({redacted})"
                ) from e
            raw_email = c.get("email_primary.email")
            email = raw_email if isinstance(raw_email, str) and raw_email else None
            result.append(
                CiviMember(
                    contact_id=cid,
                    display_name=str(c["display_name"]),
                    card_id=card_id,
                    membership_types=tuple(types_by_contact.get(cid, [])),
                    email=email,
                )
            )
        return result

    def _fetch_contacts(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            page = self._post(
                "Contact",
                "get",
                {
                    "select": [
                        "id",
                        "display_name",
                        self._config.card_id_field,
                        "email_primary.email",
                    ],
                    "where": [
                        [self._config.card_id_field, "IS NOT EMPTY"],
                        ["is_deleted", "=", False],
                    ],
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                },
            )
            results.extend(page)
            if len(page) < _PAGE_SIZE:
                return results
            offset += _PAGE_SIZE
        raise CivicrmClientError(
            f"Contact.get pagination exceeded {_MAX_PAGES} pages without terminating"
        )

    def _fetch_memberships(self, contact_ids: list[int]) -> list[dict[str, Any]]:
        if not contact_ids:
            return []
        results: list[dict[str, Any]] = []
        for start in range(0, len(contact_ids), _CONTACT_BATCH_SIZE):
            batch = contact_ids[start : start + _CONTACT_BATCH_SIZE]
            results.extend(self._fetch_memberships_for_batch(batch))
        return results

    def _fetch_memberships_for_batch(self, batch_ids: list[int]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            page = self._post(
                "Membership",
                "get",
                {
                    "select": [
                        "contact_id",
                        "membership_type_id:label",
                        "status_id:name",
                    ],
                    "where": [
                        ["contact_id", "IN", batch_ids],
                        ["status_id:name", "IN", list(self._config.active_statuses)],
                    ],
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                },
            )
            results.extend(page)
            if len(page) < _PAGE_SIZE:
                return results
            offset += _PAGE_SIZE
        raise CivicrmClientError(
            f"Membership.get pagination exceeded {_MAX_PAGES} pages without terminating"
        )

    def _post(self, entity: str, action: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{_API_PATH}/{entity}/{action}"
        data = {"params": json.dumps(params)}
        response = self._with_retries(lambda: self._http.post(url, data=data))
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise CivicrmClientError(f"malformed JSON response from {url}: {e}") from e
        if not isinstance(payload, dict):
            return []
        values = payload.get("values", [])
        if not isinstance(values, list):
            return []
        return values

    def _with_retries(self, action: Callable[[], httpx.Response]) -> httpx.Response:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = action()
            except httpx.RequestError as e:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(
                        f"network failure after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code == 429:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(
                        f"HTTP 429 (rate limited) after {_MAX_ATTEMPTS} attempts: "
                        f"{response.text[:200]}"
                    )
                wait = _parse_retry_after(response) or _backoff_seconds(attempt)
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(
                        f"HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts: "
                        f"{response.text[:200]}"
                    )
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code >= 400:
                # 4xx other than 429 → permanent, no retry
                raise CivicrmClientError(f"HTTP {response.status_code}: {response.text[:200]}")

            return response

        # Unreachable (mypy/typing only); the loop always returns or raises.
        raise CivicrmClientError("retry loop exited unexpectedly")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "CivicrmClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _coerce_card_id(raw: object) -> int | None:
    """CiviCRM may return card_id as int or string depending on the field type.

    Empty string and None map to None; numeric strings and ints parse via int().
    Callers must catch ValueError/TypeError and surface them with contact context;
    bubbling raw int() errors is the wrong layer to diagnose data issues.

    The caller has already filtered contacts to non-empty card_id, so the None
    path is defensive only.
    """
    if raw is None or raw == "":
        return None
    return int(raw)  # type: ignore[call-overload, no-any-return]


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with ±20% jitter. attempt is 1-indexed."""
    base = float(2 ** (attempt - 1))  # 1.0, 2.0, 4.0 ...
    jitter = random.uniform(-0.2, 0.2) * base
    return max(0.1, base + jitter)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header. Returns positive seconds value if numeric.

    HTTP-date form is not supported (per spec §13) and returns None.
    Negative and zero values return None so the caller falls back to backoff;
    negatives would otherwise cause time.sleep to raise ValueError.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if result > 0 else None
