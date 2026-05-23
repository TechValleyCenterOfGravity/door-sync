"""UniFi Access local-API client for door-sync.

Reads users (sync-managed users have employee_number set to their CiviCRM
contact_id) and applies a Diff: deactivates departed members, updates
credentials and policies, registers and binds new NFC cards.

This module is not pure (HTTP, TLS, optional logging in dry-run). Errors
surface as UnifiClientError; the scheduler's per-cycle try/except handles
them. See docs/architecture.md §4-§5 for the layering rules.
"""


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
