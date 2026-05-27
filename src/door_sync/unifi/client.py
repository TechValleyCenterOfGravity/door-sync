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
from collections.abc import Callable
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

    def __init__(self, config: UnifiConfig, *, dry_run: bool = False) -> None:
        """Initialize the UniFi Access client.

        Args:
            config: UniFi connection settings including host, API key, TLS fingerprint, and facility code.
            dry_run: If True, write operations log intended actions instead of executing them.
        """
        self._config = config
        self._dry_run = dry_run
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
        """
        Validate the UniFi host's TLS certificate by performing a TLS handshake and comparing the certificate's SHA-256 fingerprint to the configured fingerprint.

        Performs a TLS handshake (negotiating at least TLS 1.2) against the configured host and port, extracts the peer certificate in DER form, computes its SHA-256 fingerprint (hex, lowercase, without colons), and compares it to the normalized configured fingerprint. If no peer certificate is returned or the fingerprints do not match, raises UnifiClientError.

        Raises:
            UnifiClientError: If the TLS handshake yields no peer certificate or the computed fingerprint does not match the configured fingerprint.
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
        """
        Perform an HTTP request against the UniFi server and return the API envelope's `data` value.

        This forwards `method`, `path` (relative to the client's base URL), and optional `params`, `json`, and `files` to the underlying HTTP client, performs the request with the client's retry logic, and validates/unpacks the UniFi response envelope.

        Parameters:
            method (str): HTTP method (e.g., "GET", "POST", "PUT", "DELETE").
            path (str): Request path relative to the client's base URL (may include leading slash).
            params (dict[str, Any] | None): Query parameters to include in the request.
            json (Any): JSON-serializable body to send.
            files (dict[str, Any] | None): Files/multipart payload to send.

        Returns:
            Any: The `data` field extracted from the UniFi API response envelope on success.
        """

        def _do() -> httpx.Response:
            """
            Perform the configured HTTP request using the client's HTTPX session.

            Returns:
                httpx.Response: The HTTPX response returned by the request.
            """
            return self._http.request(method, path, params=params, json=json, files=files)

        response = self._with_retries(_do)
        return self._unwrap(response)

    def _unwrap(self, response: httpx.Response) -> Any:
        """
        Validate a UniFi API response envelope and return its `data` field.

        Parameters:
            response (httpx.Response): The HTTP response returned by the UniFi API.

        Returns:
            Any: The value of the envelope's `data` field, or `None` if the field is missing.

        Raises:
            UnifiClientError: If the response body is not valid JSON, if the JSON is not an object,
                or if the envelope `code` is not `"SUCCESS"` (the exception message includes the
                returned `code` and `msg` when available).
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
        """
        Perform the provided HTTP action with retry, backoff, and rate-limit handling.

        Parameters:
            action (Callable[[], httpx.Response]): Zero-argument callable that executes an HTTP request and returns an `httpx.Response`.

        Returns:
            httpx.Response: The successful response returned by `action` (status code less than 400).

        Raises:
            UnifiClientError: On non-retryable HTTP errors (4xx other than 429), when retries are exhausted for network failures,
            rate-limited responses (429), or server errors (5xx), or if the retry loop exits unexpectedly.
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
        """
        Fetch all UniFi users from the developer API, parse them into UnifiUser objects, and return the aggregated list.

        Pages through the /api/v1/developer/users endpoint until a page smaller than the page size is returned. Populates the client's fetched-user caches via _row_to_unifi_user and sets _fetched_users_done to True when pagination completes.

        Returns:
            list[UnifiUser]: Parsed users collected from all returned pages; rows that cannot be parsed are omitted.

        Raises:
            UnifiClientError: If the API returns a non-list payload for a page or if pagination exceeds the maximum allowed pages.
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
        """
        Convert a UniFi API user row into a UnifiUser, or return None if the row should be ignored.

        Parses `employee_number` as the managed contact ID and skips rows when the value is missing, non-integer, less than or equal to zero, or when the UniFi row lacks an `id`. When a valid user is produced, caches are updated: `_unifi_user_id_by_contact[contact_id]` is set to the UniFi `id` and `_nfc_cards_by_contact[contact_id]` is set to the row's `nfc_cards` list (empty if missing or malformed). If multiple NFC cards or access policies are present, a warning is logged and the first entry is used. NFC card identifiers are parsed with the configured facility code; a card whose facility code does not match is treated as no card and a warning is logged.

        Returns:
            UnifiUser or `None`: A UnifiUser representing the row when it maps to a managed contact, or `None` if the row should be ignored.
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

        policies = row.get("access_policy_ids") or []
        if not isinstance(policies, list):
            policies = []
        if len(policies) > 1:
            logger.warning(
                "contact %d has %d access policies; using the first",
                contact_id,
                len(policies),
            )
        policy = str(policies[0]) if policies else None

        first_name = str(row.get("first_name", ""))
        last_name = str(row.get("last_name", ""))
        display_name = " ".join(part for part in [first_name, last_name] if part).strip()
        active = str(row.get("status", "")) == "ACTIVE"

        return UnifiUser(
            contact_id=contact_id,
            display_name=display_name,
            card_id=card_id,
            active=active,
            policy=policy,
        )

    def apply(self, diff: Diff) -> None:
        """Apply a diff to UniFi Access.

        Precondition: fetch_users() must have been called on this instance
        first (the orchestrator's flow enforces this). The cached
        unifi_user_id and nfc_cards maps it populates are required.
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
        """
        Apply credential-related changes from a Diff to UniFi users.

        For each member in the diff's credential-update set, update the user's display name and/or NFC card binding in UniFi. If the contact has no cached UniFi user id the entry is skipped. When replacing a card, any existing NFC card tokens cached for the contact are deleted before binding the new card. Raises UnifiClientError if a required NFC token is unavailable after imports.

        Parameters:
            diff (Diff): The diff containing credential updates to apply.
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

            if resolved.display_name != unifi_user.display_name:
                first, last = _split_name(resolved.display_name)
                self._request(
                    "PUT",
                    f"/api/v1/developer/users/{user_id}",
                    json={"first_name": first, "last_name": last},
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
            logger.info(
                "would-update-credential contact=%d old_card=%s new_card=%s",
                resolved.contact_id,
                _redact(unifi_user.card_id),
                _redact(resolved.card_id),
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
        """
        Ensure the NFC token map is populated when the diff contains any card assignments.

        If any entry in `diff.to_add` or `diff.to_update_credential` has a non-None `card_id`, load the cached card-id→token map via `_ensure_nfc_token_map()` so dry-run reports can reference existing tokens.
        """
        any_card = any(r.card_id is not None for r in diff.to_add) or any(
            r.card_id is not None for r, _ in diff.to_update_credential
        )
        if any_card:
            self._ensure_nfc_token_map()

    def _preimport_unknown_cards(self, diff: Diff) -> None:
        """Batch-import any card_ids needed by to_add or to_update_credential
        that aren't already in the token map.
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
        """
        Load and cache a mapping of NFC card IDs to their tokens from the UniFi local API.

        Pages through the /api/v1/developer/credentials/nfc_cards/tokens endpoint, parses each row's `nfc_id` using the client's configured facility code, and builds a dict mapping the parsed numeric card ID to its token. The result is cached on the instance and returned on subsequent calls.

        Returns:
            token_map (dict[int, str]): Mapping from NFC card numeric ID to token.

        Raises:
            UnifiClientError: If the API returns an unexpected payload shape or pagination exceeds the configured maximum pages.
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
        """
        Import NFC cards by uploading a two-column CSV and update the client's NFC token map.

        If `card_ids` is empty this is a no-op. The method uploads a headerless CSV of `<nfc_id>,<alias>` rows to the UniFi import endpoint, validates the response, and updates the cached token map for each imported card.

        Raises:
            UnifiClientError: if the import response is not a list, if an imported row's `nfc_id` cannot be parsed for the configured facility code, or if an imported row contains an empty `token`.
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
        """
        Apply additions from a Diff by creating new UniFi users or reactivating existing ones, and then bind NFC credentials and assign access policies as specified.

        For each resolved member in diff.to_add:
        - If a UniFi user already exists for the contact, prepare reactivation, bind the configured NFC card (if any), assign the configured access policy (if any), and activate the user.
        - If no existing UniFi user exists, create a new user, update the internal contact->user_id cache, then bind the card and assign the policy as applicable.

        Parameters:
            diff (Diff): A Diff containing members to add; entries are read from `diff.to_add`.

        Side effects:
            Performs network calls to the UniFi local API to create/update users, bind/delete NFC cards, and assign access policies, and updates the client's internal caches.
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
        """
        Update an existing UniFi user record with the provided name and employee_number, then remove any cached NFC cards for that contact whose tokens differ from the newly bound card.

        Parameters:
            resolved (ResolvedMember): Resolved member data; `contact_id` is written into `employee_number` and `card_id` is used to determine the new card token to keep.
            user_id (str): UniFi user identifier to update.
            first (str): First name to set on the UniFi user.
            last (str): Last name to set on the UniFi user.
        """
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}",
            json={
                "first_name": first,
                "last_name": last,
                "employee_number": str(resolved.contact_id),
            },
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
        """
        Activate a UniFi user account by setting its status to "ACTIVE".

        Parameters:
            user_id (str): UniFi user resource identifier (the user's `id` as returned by the UniFi API).
        """
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}",
            json={"status": "ACTIVE"},
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _create_user(self, resolved: ResolvedMember, first: str, last: str) -> str:
        """
        Create a UniFi user for the given resolved member and return the new UniFi user id.

        Parameters:
            resolved (ResolvedMember): Member data containing the contact_id to set as employee_number.
            first (str): First name to assign to the UniFi user.
            last (str): Last name to assign to the UniFi user.

        Returns:
            str: The newly created UniFi user id.

        Raises:
            UnifiClientError: If the UniFi API response does not contain an `id` for the created user.
        """
        data = self._request(
            "POST",
            "/api/v1/developer/users",
            json={
                "first_name": first,
                "last_name": last,
                "employee_number": str(resolved.contact_id),
            },
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
        if not isinstance(data, dict) or "id" not in data:
            raise UnifiClientError(f"POST /users returned no id for contact={resolved.contact_id}")
        return str(data["id"])

    def _bind_card_if_set(self, user_id: str, resolved: ResolvedMember) -> None:
        """
        Bind the resolved NFC card to the given UniFi user when a card is specified.

        Parameters:
            user_id (str): UniFi user identifier to which the card should be bound.
            resolved (ResolvedMember): Resolved member object containing `card_id` and `contact_id`.

        Raises:
            UnifiClientError: If `resolved.card_id` is set but no token is available for that card after import.
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
        """
        Assigns the resolved access policy to the specified UniFi user if a target policy is set.

        Parameters:
            user_id (str): UniFi user identifier to update.
            resolved (ResolvedMember): Member record whose `target_policy`, if not `None`, will be applied to the user.
        """
        if resolved.target_policy is None:
            return
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
