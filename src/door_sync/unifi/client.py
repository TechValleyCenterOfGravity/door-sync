"""UniFi Access local-API client for door-sync.

Reads users (sync-managed users have employee_number set to their CiviCRM
contact_id) and applies a Diff: deactivates departed members, updates
credentials and policies, registers and binds new NFC cards.

This module is not pure (HTTP, TLS, optional logging in dry-run). Errors
surface as UnifiClientError; the scheduler's per-cycle try/except handles
them. See docs/architecture.md §4-§5 for the layering rules.
"""

import hashlib
import socket
import ssl
from types import TracebackType

import httpx

from door_sync.config import UnifiConfig

_UNIFI_PORT = 12445


class UnifiClientError(Exception):
    """Raised on non-recoverable UniFi Access API failure."""


class UnifiClient:
    """Read+write UniFi Access local-API client.

    Construct one per reconcile cycle. Use as a context manager, or call
    close() explicitly. Honors a dry_run flag that turns writes into
    redacted log lines (architecture §5).
    """

    def __init__(self, config: UnifiConfig, *, dry_run: bool = False) -> None:
        self._config = config
        self._dry_run = dry_run
        self._verify_tls_fingerprint()
        self._http = httpx.Client(
            base_url=f"https://{config.host}:{_UNIFI_PORT}",
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=False,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    def _verify_tls_fingerprint(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection(
            (self._config.host, _UNIFI_PORT), timeout=10
        ) as raw:
            with ctx.wrap_socket(raw, server_hostname=self._config.host) as wrapped:
                cert_der = wrapped.getpeercert(binary_form=True)
        if cert_der is None:
            raise UnifiClientError("TLS handshake produced no peer certificate")
        actual_fp = hashlib.sha256(cert_der).hexdigest().lower()
        expected_fp = (
            self._config.tls_fingerprint.lower().replace(":", "")
        )
        if actual_fp != expected_fp:
            raise UnifiClientError(
                f"TLS fingerprint mismatch: expected {expected_fp[:16]}…, "
                f"got {actual_fp[:16]}…"
            )

    def close(self) -> None:
        # httpx.Client may not exist if __init__ failed before constructing it.
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
