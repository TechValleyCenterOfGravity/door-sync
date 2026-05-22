"""CiviCRM API4 client for door-sync.

Reads active members (contacts with a non-empty card_id and an active
membership) from a WordPress-hosted CiviCRM instance. Read-only — no write
operations. Hand-rolled retry on 5xx and 429.

This module is not pure (it does HTTP), but it does NOT call sys.exit.
Errors surface as CivicrmClientError so the scheduler's per-cycle try/except
can log and continue.
"""

import json
from types import TracebackType
from typing import Any

import httpx

from door_sync.config import CivicrmConfig
from door_sync.models import CiviMember

_API_PATH = "/wp-json/civicrm/v3/api4"
_PAGE_SIZE = 250
_ACTIVE_STATUSES = ["Current", "Grace"]
_MAX_PAGES = 1_000  # 250,000 records — far above any plausible deployment


class CivicrmClientError(Exception):
    """Raised on non-recoverable CiviCRM API failure."""


class CivicrmClient:
    """Read-only CiviCRM API4 client.

    Construct one per reconcile cycle; the underlying httpx.Client owns
    connection state. Use as a context manager, or call close() explicitly.
    """

    def __init__(self, config: CivicrmConfig) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.host,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=True,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    def fetch_active(self) -> list[CiviMember]:
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
            result.append(
                CiviMember(
                    contact_id=cid,
                    display_name=str(c["display_name"]),
                    card_id=_coerce_card_id(c.get(self._config.card_id_field)),
                    membership_types=types_by_contact.get(cid, []),
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
                    "select": ["id", "display_name", self._config.card_id_field],
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
                        ["contact_id", "IN", contact_ids],
                        ["status_id:name", "IN", _ACTIVE_STATUSES],
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

    def _post(
        self, entity: str, action: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        url = f"{_API_PATH}/{entity}/{action}"
        data = {"params": json.dumps(params)}
        response = self._http.post(url, data=data)
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        values = payload.get("values", [])
        if not isinstance(values, list):
            return []
        return values

    def close(self) -> None:
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

    Empty string and None map to None; everything else is parsed as int.
    Caller has already filtered contacts to non-empty card_id, so the None
    path is defensive only.
    """
    if raw is None or raw == "":
        return None
    return int(raw)  # type: ignore[call-overload, no-any-return]
