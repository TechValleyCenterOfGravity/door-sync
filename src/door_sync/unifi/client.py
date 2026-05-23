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
