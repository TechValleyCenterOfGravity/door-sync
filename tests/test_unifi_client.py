"""Tests for the UniFi Access client."""

from door_sync.unifi.client import (
    _compute_nfc_id,
    _parse_nfc_id,
    _redact,
    _split_name,
)

# --- Card-ID encoding helpers ---


def test_compute_nfc_id_known_values() -> None:
    """Verified encoding: (FC << 16) | CN as uppercase hex, no padding."""
    assert _compute_nfc_id(42, 1234) == "2A04D2"
    assert _compute_nfc_id(42, 1235) == "2A04D3"


def test_compute_nfc_id_zero_card_number() -> None:
    """CN=0 still produces FC-prefixed hex, not just '0'."""
    assert _compute_nfc_id(42, 0) == "2A0000"


def test_parse_nfc_id_matching_facility_code() -> None:
    """Inverse of _compute_nfc_id for the same FC."""
    assert _parse_nfc_id("2A04D2", 42) == 1234
    assert _parse_nfc_id("2A04D3", 42) == 1235


def test_parse_nfc_id_mismatched_facility_code_returns_none() -> None:
    """Foreign-FC cards are out of our namespace; signal via None."""
    assert _parse_nfc_id("2A04D2", 99) is None


def test_parse_nfc_id_garbage_returns_none() -> None:
    """Unparseable strings return None instead of raising."""
    assert _parse_nfc_id("not-hex", 42) is None
    assert _parse_nfc_id("", 42) is None


def test_parse_nfc_id_lowercase_hex_still_parses() -> None:
    """Defensive: don't trust UniFi to always uppercase the response."""
    assert _parse_nfc_id("2a04d3", 42) == 1235


# --- Name splitting ---


def test_split_name_two_words() -> None:
    assert _split_name("Jane Doe") == ("Jane", "Doe")


def test_split_name_three_words_splits_on_last_space() -> None:
    """A middle name or compound first name belongs with first_name."""
    assert _split_name("Mary Anne Doe") == ("Mary Anne", "Doe")


def test_split_name_single_word_pads_last_name() -> None:
    """UniFi requires both fields on create; em-dash flags it for review."""
    assert _split_name("Madonna") == ("Madonna", "—")


def test_split_name_empty_string_raises() -> None:
    """Empty display_name should never reach us; defensive."""
    import pytest
    with pytest.raises(ValueError):
        _split_name("")


# --- Card-ID redaction ---


def test_redact_none() -> None:
    assert _redact(None) == "none"


def test_redact_short_card_id_zero_pads() -> None:
    """Card 7 redacts to ****0007, not ****7."""
    assert _redact(7) == "****0007"


def test_redact_full_width_card_id() -> None:
    assert _redact(1234) == "****1234"


def test_redact_strips_high_digits() -> None:
    """Only the last 4 digits ever appear in logs."""
    assert _redact(98765) == "****8765"
