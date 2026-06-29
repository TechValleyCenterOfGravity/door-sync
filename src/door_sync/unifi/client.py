"""UniFi Access local-API client for door-sync.

Reads users (sync-managed users have employee_number set to their CiviCRM
contact_id) and applies a Diff: deactivates departed members, updates
credentials and policies, registers and binds new NFC cards.

This module is not pure (HTTP, TLS, optional logging in dry-run). Errors
surface as UnifiClientError; the scheduler's per-cycle try/except handles
them. See docs/architecture.md §4-§5 for the layering rules.
"""

import hashlib
import json as _json
import logging
import random
import socket
import ssl
import time
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx

from door_sync.config import UnifiConfig
from door_sync.models import Diff, ResolvedMember, UnifiUser

_UNIFI_PORT = 12445
_MAX_ATTEMPTS = 3
_MAX_PAGES = 1_000
_PAGE_SIZE = 100

logger = logging.getLogger(__name__)


class UnifiClientError(Exception):
    """Raised on non-recoverable UniFi Access API failure."""


class UnifiClient:
    """Read+write UniFi Access local-API client.

    Construct one per reconcile cycle. Use as a context manager, or call
    close() explicitly. Honors a dry_run flag that turns writes into
    redacted log lines (architecture §5).
    """

    def __init__(
        self,
        config: UnifiConfig,
        *,
        dry_run: bool = False,
        managed_policy_ids: Iterable[str] | None = None,
    ) -> None:
        """Initialize the UniFi Access client.

        Args:
            config: UniFi connection settings including host, API key, TLS fingerprint, and facility code.
            dry_run: If True, write operations log intended actions instead of executing them.
            managed_policy_ids: The set of access policy IDs door-sync owns (the
                tier-mapping target policies). When provided, any policy on a
                UniFi user that is not in this set is treated as externally
                managed (e.g. a policy auto-applied to all users): it is ignored
                when reading the user's current policy and preserved on write.
                When None/empty, every policy is treated as managed (legacy
                behavior: take the first).
        """
        self._config = config
        self._dry_run = dry_run
        self._managed_policy_ids: frozenset[str] = frozenset(managed_policy_ids or ())
        # Resolve hostname+port once so TLS verification and httpx requests
        # both target the same endpoint. Without this, a host like
        # "https://controller.example.org" (no port) would pin TLS on 12445
        # but send API calls to 443.
        parsed = urlsplit(config.host)
        self._hostname = parsed.hostname or config.host
        self._port = parsed.port or _UNIFI_PORT
        scheme = parsed.scheme or "https"
        self._verify_tls_fingerprint()
        self._http = httpx.Client(
            base_url=f"{scheme}://{self._hostname}:{self._port}",
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=False,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        self._unifi_user_id_by_contact: dict[int, str] = {}
        self._nfc_cards_by_contact: dict[int, list[dict[str, Any]]] = {}
        self._nfc_token_map: dict[int, str] | None = None
        self._fetched_users_done = False

    def _verify_tls_fingerprint(self) -> None:
        """Validate the controller's TLS certificate against the configured fingerprint.

        Raises:
            UnifiClientError: If no peer certificate is returned or the
                fingerprint does not match.
        """
        hostname = self._hostname
        port = self._port
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # Defense in depth: even with fingerprint pinning, refuse to negotiate
        # TLS 1.0 / 1.1. Modern Python+OpenSSL defaults are already 1.2+, but
        # setting this explicitly silences CodeQL and guards older builds.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.create_connection((hostname, port), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=hostname) as wrapped:
                cert_der = wrapped.getpeercert(binary_form=True)
        if cert_der is None:
            raise UnifiClientError("TLS handshake produced no peer certificate")
        actual_fp = hashlib.sha256(cert_der).hexdigest().lower()
        expected_fp = self._config.tls_fingerprint.lower().replace(":", "")
        if actual_fp != expected_fp:
            raise UnifiClientError(
                f"TLS fingerprint mismatch: expected {expected_fp[:16]}…, got {actual_fp[:16]}…"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an HTTP request and return the API envelope's `data` value.

        Args:
            method: HTTP method (e.g., "GET", "POST", "PUT", "DELETE").
            path: Request path relative to the client's base URL.
            params: Query parameters to include in the request.
            json: JSON-serializable body to send.
            files: Multipart payload to send.

        Returns:
            The `data` field from the UniFi API response envelope.
        """

        def _do() -> httpx.Response:
            return self._http.request(method, path, params=params, json=json, files=files)

        response = self._with_retries(_do)
        return self._unwrap(response)

    def _unwrap(self, response: httpx.Response) -> Any:
        """Validate a UniFi API response envelope and return its `data` field.

        Args:
            response: The HTTP response from the UniFi API.

        Returns:
            The envelope's `data` field, or None if missing.

        Raises:
            UnifiClientError: If the response is not valid JSON, not an object,
                or the envelope `code` is not "SUCCESS".
        """
        try:
            payload = response.json()
        except (ValueError, _json.JSONDecodeError) as e:
            raise UnifiClientError(f"malformed JSON from {response.url}: {e}") from e
        if not isinstance(payload, dict):
            raise UnifiClientError(
                f"unexpected envelope shape from {response.url}: "
                f"expected object, got {type(payload).__name__}"
            )
        code = payload.get("code")
        if code != "SUCCESS":
            msg = payload.get("msg", "")
            raise UnifiClientError(f"{code}: {msg}")
        return payload.get("data")

    def _with_retries(self, action: Callable[[], httpx.Response]) -> httpx.Response:
        """Execute an HTTP action with retry, backoff, and rate-limit handling.

        Args:
            action: Zero-argument callable that performs the HTTP request.

        Returns:
            The successful response (status code below 400).

        Raises:
            UnifiClientError: On exhausted retries or non-retryable 4xx errors.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = action()
            except httpx.RequestError as e:
                if attempt == _MAX_ATTEMPTS:
                    raise UnifiClientError(
                        f"network failure after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code == 429:
                if attempt == _MAX_ATTEMPTS:
                    raise UnifiClientError(
                        f"HTTP 429 after {_MAX_ATTEMPTS} attempts: {response.text[:200]}"
                    )
                wait = _parse_retry_after(response) or _backoff_seconds(attempt)
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                if attempt == _MAX_ATTEMPTS:
                    raise UnifiClientError(
                        f"HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts: "
                        f"{response.text[:200]}"
                    )
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code >= 400:
                # 4xx other than 429 (including non-standard 402) → permanent.
                raise UnifiClientError(f"HTTP {response.status_code}: {response.text[:200]}")

            return response

        raise UnifiClientError("retry loop exited unexpectedly")

    def fetch_users(self) -> list[UnifiUser]:
        """Fetch all UniFi users and parse them into `UnifiUser` objects.

        Pages through the `/api/v1/developer/users` endpoint and populates
        internal caches for user IDs and NFC cards.

        Returns:
            Parsed users from all pages; rows that cannot be parsed are omitted.

        Raises:
            UnifiClientError: If the API returns a non-list payload or pagination
                exceeds the maximum allowed pages.
        """
        results: list[UnifiUser] = []
        for page_num in range(1, _MAX_PAGES + 1):
            data = self._request(
                "GET",
                "/api/v1/developer/users",
                params={
                    "page_num": page_num,
                    "page_size": _PAGE_SIZE,
                    "expand[]": "access_policy",
                },
            )
            if not isinstance(data, list):
                raise UnifiClientError(
                    f"expected list of users from /users, got {type(data).__name__}"
                )
            for row in data:
                user = self._row_to_unifi_user(row)
                if user is not None:
                    results.append(user)
            if len(data) < _PAGE_SIZE:
                self._fetched_users_done = True
                return results
        raise UnifiClientError(f"/users pagination exceeded {_MAX_PAGES} pages without terminating")

    def _row_to_unifi_user(self, row: dict[str, Any]) -> UnifiUser | None:
        """Convert a UniFi API user row into a `UnifiUser`, or None if unmanaged.

        Parses `employee_number` as the managed contact ID. Skips rows with
        missing, non-integer, or non-positive values. Updates internal caches
        for user IDs and NFC cards when a valid user is produced.

        Args:
            row: Raw user dict from the UniFi API.

        Returns:
            A `UnifiUser` for managed contacts, or None for unmanaged rows.
        """
        emp_raw = row.get("employee_number") or ""
        try:
            contact_id = int(emp_raw)
        except (ValueError, TypeError):
            return None
        # CiviCRM contact_ids are positive integers (auto-increment from 1).
        # A UniFi user with employee_number "0" or negative was not provisioned
        # by this reconciler; treat it as admin-managed and skip. Without this
        # guard, such a user would land in to_deactivate on every cycle since
        # no ResolvedMember will ever match.
        if contact_id <= 0:
            return None
        user_id = str(row.get("id", ""))
        if not user_id:
            return None
        self._unifi_user_id_by_contact[contact_id] = user_id

        nfc_cards = row.get("nfc_cards") or []
        if not isinstance(nfc_cards, list):
            nfc_cards = []
        self._nfc_cards_by_contact[contact_id] = list(nfc_cards)

        card_id: int | None = None
        if len(nfc_cards) > 1:
            logger.warning(
                "contact %d has %d cards in UniFi; using the first",
                contact_id,
                len(nfc_cards),
            )
        if nfc_cards:
            nfc_id_raw = str(nfc_cards[0].get("nfc_id", ""))
            card_id = _parse_nfc_id(nfc_id_raw, self._config.facility_code)
            if card_id is None and nfc_id_raw:
                logger.warning(
                    "contact %d has foreign-FC card; treating as no card",
                    contact_id,
                )

        raw_policies = row.get("access_policy_ids") or []
        if not isinstance(raw_policies, list):
            raw_policies = []
        all_policies = [str(p) for p in raw_policies]
        # When a managed set is configured, only policies door-sync owns count as
        # "the user's policy"; anything else (e.g. a policy UniFi auto-applies to
        # all users) is ignored so it isn't mistaken for tier drift. With no
        # managed set, every policy is treated as managed (legacy behavior).
        if self._managed_policy_ids:
            managed = [p for p in all_policies if p in self._managed_policy_ids]
        else:
            managed = all_policies
        if len(managed) > 1:
            logger.warning(
                "contact %d has %d access policies; using the first",
                contact_id,
                len(managed),
            )
        policy = managed[0] if managed else None

        first_name = str(row.get("first_name", ""))
        last_name = str(row.get("last_name", ""))
        display_name = " ".join(part for part in [first_name, last_name] if part).strip()
        active = str(row.get("status", "")) == "ACTIVE"

        email_raw = row.get("user_email") or ""
        email = str(email_raw) if email_raw else None

        return UnifiUser(
            contact_id=contact_id,
            display_name=display_name,
            card_id=card_id,
            active=active,
            policy=policy,
            email=email,
        )

    def apply(self, diff: Diff) -> None:
        """Apply a diff to UniFi Access.

        Precondition: `fetch_users()` must have been called on this instance
        first (the orchestrator's flow enforces this). The cached
        `_unifi_user_id_by_contact` and `_nfc_cards_by_contact` maps it
        populates are required.

        Args:
            diff: The reconciliation diff to apply (adds, updates, deactivations).
        """
        if not self._fetched_users_done:
            raise UnifiClientError(
                "apply() requires a prior fetch_users() call on the same instance"
            )
        if self._dry_run:
            # Even in dry-run, exercise the read paths so a dry-run report
            # reflects which cards would need to be imported (spec §8).
            self._populate_token_map_for_dry_run(diff)
            self._log_dry_run_actions(diff)
            return
        self._preimport_unknown_cards(diff)
        self._apply_deactivate(diff)
        self._apply_update_credential(diff)
        self._apply_update_policy(diff)
        self._apply_add(diff)

    _INTER_CALL_DELAY_SECONDS = 0.075

    def _apply_update_credential(self, diff: Diff) -> None:
        """Apply credential changes (display name and/or NFC card) from a diff.

        Args:
            diff: The diff containing credential updates to apply.
        """
        if not diff.to_update_credential:
            return

        for resolved, unifi_user in diff.to_update_credential:
            user_id = self._unifi_user_id_by_contact.get(resolved.contact_id)
            if user_id is None:
                logger.warning(
                    "skipping update_credential for contact=%d: no cached user_id",
                    resolved.contact_id,
                )
                continue

            user_fields: dict[str, Any] = {}
            if resolved.display_name != unifi_user.display_name:
                first, last = _split_name(resolved.display_name)
                user_fields["first_name"] = first
                user_fields["last_name"] = last
            if _email_differs_ci(resolved.email, unifi_user.email):
                # Empty string clears the email in UniFi; a value sets it.
                user_fields["user_email"] = resolved.email or ""
            if user_fields:
                self._request(
                    "PUT",
                    f"/api/v1/developer/users/{user_id}",
                    json=user_fields,
                )
                time.sleep(self._INTER_CALL_DELAY_SECONDS)

            if resolved.card_id != unifi_user.card_id:
                # Delete old card(s) on the user.
                for old_card in self._nfc_cards_by_contact.get(resolved.contact_id, []):
                    old_token = str(old_card.get("token", ""))
                    if not old_token:
                        continue
                    self._request(
                        "DELETE",
                        f"/api/v1/developer/users/{user_id}/nfc_cards/delete",
                        json={"token": old_token},
                    )
                    time.sleep(self._INTER_CALL_DELAY_SECONDS)
                # Bind new card if specified.
                if resolved.card_id is not None:
                    new_token = self._ensure_nfc_token_map().get(resolved.card_id)
                    if new_token is None:
                        raise UnifiClientError(
                            f"no token for card_id={_redact(resolved.card_id)} "
                            f"after import (contact={resolved.contact_id})"
                        )
                    self._request(
                        "PUT",
                        f"/api/v1/developer/users/{user_id}/nfc_cards",
                        json={"token": new_token, "force_add": False},
                    )
                    time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _apply_update_policy(self, diff: Diff) -> None:
        for resolved, _unifi_user in diff.to_update_policy:
            user_id = self._unifi_user_id_by_contact.get(resolved.contact_id)
            if user_id is None:
                logger.warning(
                    "skipping update_policy for contact=%d: no cached user_id",
                    resolved.contact_id,
                )
                continue
            if resolved.target_policy is None:
                logger.warning(
                    "skipping update_policy for contact=%d: target_policy is None",
                    resolved.contact_id,
                )
                continue
            # Send ONLY the tier policy. A policy UniFi auto-applies to all
            # users is intentionally omitted: this endpoint sets per-user
            # assignments, so including the global ID would convert it into a
            # manual per-user mapping. The global policy auto-applies on its own.
            self._request(
                "PUT",
                f"/api/v1/developer/users/{user_id}/access_policies",
                json={"access_policy_ids": [resolved.target_policy]},
            )
            time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _apply_deactivate(self, diff: Diff) -> None:
        for unifi_user in diff.to_deactivate:
            user_id = self._unifi_user_id_by_contact.get(unifi_user.contact_id)
            if user_id is None:
                logger.warning(
                    "skipping deactivate for contact=%d: no cached user_id",
                    unifi_user.contact_id,
                )
                continue
            self._request(
                "PUT",
                f"/api/v1/developer/users/{user_id}",
                json={"status": "DEACTIVATED"},
            )
            time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _log_dry_run_actions(self, diff: Diff) -> None:
        # Emit would-import lines for cards not yet in the token map.
        token_map = self._nfc_token_map or {}
        needed: set[int] = set()
        for resolved in diff.to_add:
            if resolved.card_id is not None and resolved.card_id not in token_map:
                needed.add(resolved.card_id)
        for resolved, _ in diff.to_update_credential:
            if resolved.card_id is not None and resolved.card_id not in token_map:
                needed.add(resolved.card_id)
        for card_id in sorted(needed):
            logger.info("would-import card=%s", _redact(card_id))

        for member in diff.to_add:
            logger.info(
                "would-add contact=%d card=%s policy=%s",
                member.contact_id,
                _redact(member.card_id),
                member.target_policy,
            )
        for resolved, unifi_user in diff.to_update_credential:
            email_change = (
                " email-change" if _email_differs_ci(resolved.email, unifi_user.email) else ""
            )
            logger.info(
                "would-update-credential contact=%d old_card=%s new_card=%s%s",
                resolved.contact_id,
                _redact(unifi_user.card_id),
                _redact(resolved.card_id),
                email_change,
            )
        for resolved, unifi_user in diff.to_update_policy:
            logger.info(
                "would-update-policy contact=%d old=%s new=%s",
                resolved.contact_id,
                unifi_user.policy,
                resolved.target_policy,
            )
        for unifi_user in diff.to_deactivate:
            logger.info(
                "would-deactivate contact=%d card=%s",
                unifi_user.contact_id,
                _redact(unifi_user.card_id),
            )

    def _populate_token_map_for_dry_run(self, diff: Diff) -> None:
        """Load the NFC token map if the diff contains any card assignments.

        Args:
            diff: The diff to inspect for card assignments.
        """
        any_card = any(r.card_id is not None for r in diff.to_add) or any(
            r.card_id is not None for r, _ in diff.to_update_credential
        )
        if any_card:
            self._ensure_nfc_token_map()

    def _preimport_unknown_cards(self, diff: Diff) -> None:
        """Batch-import card IDs needed by `to_add` or `to_update_credential` not in the token map.

        Args:
            diff: The diff containing entries that may require new card imports.
        """
        if not diff.to_add and not diff.to_update_credential:
            return
        token_map = self._ensure_nfc_token_map()
        needed: set[int] = set()
        for resolved in diff.to_add:
            if resolved.card_id is not None and resolved.card_id not in token_map:
                needed.add(resolved.card_id)
        for resolved, _unifi_user in diff.to_update_credential:
            if resolved.card_id is not None and resolved.card_id not in token_map:
                needed.add(resolved.card_id)
        if needed:
            self._import_cards(sorted(needed))

    def _ensure_nfc_token_map(self) -> dict[int, str]:
        """Load and cache a mapping of NFC card IDs to tokens from the UniFi API.

        Returns:
            Mapping from NFC card numeric ID to token string.

        Raises:
            UnifiClientError: If the API returns an unexpected payload or
                pagination exceeds the maximum allowed pages.
        """
        if self._nfc_token_map is not None:
            return self._nfc_token_map
        token_map: dict[int, str] = {}
        for page_num in range(1, _MAX_PAGES + 1):
            data = self._request(
                "GET",
                "/api/v1/developer/credentials/nfc_cards/tokens",
                params={"page_num": page_num, "page_size": _PAGE_SIZE},
            )
            if not isinstance(data, list):
                raise UnifiClientError(
                    f"expected list of cards from /nfc_cards/tokens, got {type(data).__name__}"
                )
            for row in data:
                nfc_id = str(row.get("nfc_id", ""))
                token = str(row.get("token", ""))
                if not nfc_id or not token:
                    continue
                card_id = _parse_nfc_id(nfc_id, self._config.facility_code)
                if card_id is None:
                    logger.debug(
                        "skipping foreign-FC or unparseable card",
                    )
                    continue
                token_map[card_id] = token
            if len(data) < _PAGE_SIZE:
                break
        else:
            raise UnifiClientError(f"/nfc_cards/tokens pagination exceeded {_MAX_PAGES} pages")
        self._nfc_token_map = token_map
        return token_map

    def _import_cards(self, card_ids: list[int]) -> None:
        """Import NFC cards by uploading a CSV and update the token map.

        Args:
            card_ids: Card IDs to import. No-op if empty.

        Raises:
            UnifiClientError: If the import response is malformed or contains
                unparseable or empty-token entries.
        """
        if not card_ids:
            return
        token_map = self._ensure_nfc_token_map()
        lines: list[str] = []
        for card_id in card_ids:
            nfc_id = _compute_nfc_id(self._config.facility_code, card_id)
            alias = f"sync-{card_id:05d}"
            lines.append(f"{nfc_id},{alias}")
        csv_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        data = self._request(
            "POST",
            "/api/v1/developer/credentials/nfc_cards/import",
            files={"file": ("cards.csv", csv_bytes, "text/csv")},
        )
        if not isinstance(data, list):
            raise UnifiClientError(
                f"expected list from /nfc_cards/import, got {type(data).__name__}"
            )
        for row in data:
            nfc_id = str(row.get("nfc_id", ""))
            token = str(row.get("token", ""))
            parsed_card_id = _parse_nfc_id(nfc_id, self._config.facility_code)
            if parsed_card_id is None:
                # Don't include the raw nfc_id in the error — it encodes the
                # card number (architecture §11). For FC mismatch, log only
                # the FC byte (0-255 is not credential material). For
                # unparseable hex, log the structural failure without the
                # string.
                try:
                    bad_fc = (int(nfc_id, 16) >> 16) & 0xFF
                    detail = f"got FC {bad_fc}, expected {self._config.facility_code}"
                except ValueError:
                    detail = "nfc_id is not valid hex"
                raise UnifiClientError(f"import response card failed validation: {detail}")
            if not token:
                raise UnifiClientError(
                    f"card import failed for card_id={_redact(parsed_card_id)} (empty token in response)"
                )
            token_map[parsed_card_id] = token

    def _apply_add(self, diff: Diff) -> None:
        """Create or reactivate UniFi users for each member in `diff.to_add`.

        For existing contacts, prepares reactivation and activates. For new
        contacts, creates the user. Both paths bind NFC cards and assign
        access policies as specified.

        Args:
            diff: The diff containing members to add.
        """
        for resolved in diff.to_add:
            existing_user_id = self._unifi_user_id_by_contact.get(resolved.contact_id)
            first, last = _split_name(resolved.display_name)
            if existing_user_id is not None:
                # Reactivate path: prepare credentials/policy first, then activate.
                self._prepare_reactivation(resolved, existing_user_id, first, last)
                self._bind_card_if_set(existing_user_id, resolved)
                self._assign_policy_if_set(existing_user_id, resolved)
                self._activate_user(existing_user_id)
            else:
                # True create
                user_id = self._create_user(resolved, first, last)
                self._unifi_user_id_by_contact[resolved.contact_id] = user_id
                # Common tail for newly created users.
                self._bind_card_if_set(user_id, resolved)
                self._assign_policy_if_set(user_id, resolved)

    def _prepare_reactivation(
        self,
        resolved: ResolvedMember,
        user_id: str,
        first: str,
        last: str,
    ) -> None:
        """Update an existing user's name, employee number, and email, then remove stale NFC cards.

        Sets first_name, last_name, employee_number, and user_email
        unconditionally. Reactivation targets an existing record that may carry
        a stale email, so an absent email is sent as an empty string to clear
        it in the same cycle — matching the credential-update path. (True-create
        omits user_email instead: a new record has nothing to clear.) Stale NFC
        cards (any card whose token differs from the new card's token) are
        deleted so the bind step starts from a clean slate.

        Args:
            resolved: Resolved member data for the contact being reactivated.
            user_id: UniFi user identifier to update.
            first: First name to set.
            last: Last name to set.
        """
        body: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
            "employee_number": str(resolved.contact_id),
            "user_email": resolved.email or "",
        }
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}",
            json=body,
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
        # Delete any old cards that differ from the new card_id.
        cached_cards = self._nfc_cards_by_contact.get(resolved.contact_id, [])
        new_token = (
            self._ensure_nfc_token_map().get(resolved.card_id)
            if resolved.card_id is not None
            else None
        )
        for old_card in cached_cards:
            old_token = str(old_card.get("token", ""))
            if not old_token or old_token == new_token:
                continue
            self._request(
                "DELETE",
                f"/api/v1/developer/users/{user_id}/nfc_cards/delete",
                json={"token": old_token},
            )
            time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _activate_user(self, user_id: str) -> None:
        """Set a UniFi user's status to ACTIVE.

        Args:
            user_id: UniFi user identifier.
        """
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}",
            json={"status": "ACTIVE"},
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _create_user(self, resolved: ResolvedMember, first: str, last: str) -> str:
        """Create a UniFi user and return the new user ID.

        Args:
            resolved: Member data; `contact_id` is set as `employee_number`.
            first: First name to assign.
            last: Last name to assign.

        Returns:
            The newly created UniFi user ID.

        Raises:
            UnifiClientError: If the API response does not contain an `id`.
        """
        body: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
            "employee_number": str(resolved.contact_id),
        }
        if resolved.email is not None:
            body["user_email"] = resolved.email
        data = self._request("POST", "/api/v1/developer/users", json=body)
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
        if not isinstance(data, dict) or "id" not in data:
            raise UnifiClientError(f"POST /users returned no id for contact={resolved.contact_id}")
        return str(data["id"])

    def _bind_card_if_set(self, user_id: str, resolved: ResolvedMember) -> None:
        """Bind the resolved NFC card to a UniFi user, if a card is specified.

        Args:
            user_id: UniFi user identifier.
            resolved: Resolved member containing `card_id` and `contact_id`.

        Raises:
            UnifiClientError: If the card has no token after import.
        """
        if resolved.card_id is None:
            return
        token = self._ensure_nfc_token_map().get(resolved.card_id)
        if token is None:
            raise UnifiClientError(
                f"no token for card_id={_redact(resolved.card_id)} "
                f"after import (contact={resolved.contact_id})"
            )
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}/nfc_cards",
            json={"token": token, "force_add": False},
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _assign_policy_if_set(self, user_id: str, resolved: ResolvedMember) -> None:
        """Assign the resolved access policy to a UniFi user, if set.

        Args:
            user_id: UniFi user identifier.
            resolved: Member record whose `target_policy` will be applied.
        """
        if resolved.target_policy is None:
            return
        # Only the tier policy; a policy auto-applied to all users is omitted
        # (see _apply_update_policy).
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}/access_policies",
            json={"access_policy_ids": [resolved.target_policy]},
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def close(self) -> None:
        """Close the underlying HTTP client, if it was successfully created."""
        http = getattr(self, "_http", None)
        if http is not None:
            http.close()

    def __enter__(self) -> "UnifiClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _compute_nfc_id(facility_code: int, card_id: int) -> str:
    """Encode a Wiegand-26 (FC, CN) pair the way UniFi exposes it in nfc_id.

    Uppercase hex of (FC << 16) | CN, no zero-padding.
    Example: (42, 1234) -> "2A04D2", (42, 1235) -> "2A04D3".
    """
    return f"{(facility_code << 16) | card_id:X}"


def _parse_nfc_id(nfc_id: str, expected_facility_code: int) -> int | None:
    """Decode UniFi's nfc_id back to a Wiegand card_number (CN).

    Returns the CN if the encoded facility code matches expected_facility_code.
    Returns None on parse failure or FC mismatch — the latter is treated as
    "card not in our namespace" and surfaces as a warning at the call site.
    """
    try:
        value = int(nfc_id, 16)
    except ValueError:
        return None
    fc = (value >> 16) & 0xFF
    cn = value & 0xFFFF
    if fc != expected_facility_code:
        return None
    return cn


def _email_differs_ci(a: str | None, b: str | None) -> bool:
    """Case-insensitive email comparison; empty string and None are equal.

    Duplicated from reconciler by design — importing reconciler here would
    violate the strict layering in architecture §4. Both copies must agree.
    """
    na = a.lower() if a else None
    nb = b.lower() if b else None
    return na != nb


def _split_name(display_name: str) -> tuple[str, str]:
    """Split a CiviCRM display_name into (first_name, last_name) for UniFi.

    Splits on the last space: 'Mary Anne Doe' -> ('Mary Anne', 'Doe').
    Single-word names get '—' as a placeholder last_name (UniFi requires
    both on create; the em-dash is visibly distinct so an operator
    notices and can edit in CiviCRM).
    """
    if not display_name:
        raise ValueError("display_name must be non-empty")
    if " " not in display_name:
        return (display_name, "—")
    first, _, last = display_name.rpartition(" ")
    return (first, last)


def _redact(card_id: int | None) -> str:
    """Return a last-4 redacted form of a card_id for log lines.

    None -> 'none'. Card_id -> '****NNNN' (zero-padded to 4 digits).
    Architecture §11: never log a full card_id at any level.
    """
    if card_id is None:
        return "none"
    return f"****{card_id % 10000:04d}"


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with ±20% jitter. attempt is 1-indexed."""
    base = float(2 ** (attempt - 1))
    jitter = random.uniform(-0.2, 0.2) * base
    return max(0.1, base + jitter)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header. Returns positive seconds if numeric.

    HTTP-date form is not supported (per spec) and returns None.
    Negative and zero values return None so the caller falls back to backoff.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if result > 0 else None
