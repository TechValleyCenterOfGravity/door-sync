"""Tests for door_sync.audit — append-only JSONL audit log."""

import json
from pathlib import Path
from typing import Any

from door_sync import audit
from door_sync.models import Diff, ResolvedMember, UnifiUser


def _resolved(contact_id: int, card_id: int | None = None,
              policy: str | None = "p1") -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=f"User {contact_id}",
        card_id=card_id,
        target_policy=policy,
        resolution="tier",
    )


def _unifi(contact_id: int, card_id: int | None = None,
           active: bool = True, policy: str | None = "p1") -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=f"User {contact_id}",
        card_id=card_id,
        active=active,
        policy=policy,
    )


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_log_applied_writes_single_jsonl_record(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    diff = Diff(
        to_add=[_resolved(1, card_id=0x1234)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )

    audit.log_applied(diff, dry_run=False, path=path, facility_code=42)

    records = _read_lines(path)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "applied"
    assert rec["dry_run"] is False
    assert rec["summary"] == {
        "added": 1,
        "updated_credential": 0,
        "updated_policy": 0,
        "deactivated": 0,
        "unmapped": 0,
    }


def test_log_applied_dry_run_marks_record(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    diff = Diff([], [], [], [], [])

    audit.log_applied(diff, dry_run=True, path=path, facility_code=42)

    records = _read_lines(path)
    assert records[0]["event"] == "applied"
    assert records[0]["dry_run"] is True


def test_log_halt_includes_reason(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    diff = Diff([], [], [], [_unifi(1, card_id=0x5678)], [])

    audit.log_halt(
        "mass_deactivate exceeded 15% threshold",
        diff, dry_run=False, path=path, facility_code=42,
    )

    rec = _read_lines(path)[0]
    assert rec["event"] == "halted"
    assert rec["reason"] == "mass_deactivate exceeded 15% threshold"
    assert rec["summary"]["deactivated"] == 1


def test_log_crashed_records_exception_class_and_message(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    audit.log_crashed(
        ConnectionError("timed out connecting to civicrm.example.org"),
        path=path,
    )

    rec = _read_lines(path)[0]
    assert rec["event"] == "crashed"
    assert rec["dry_run"] is False
    assert rec["exception"] == {
        "class": "ConnectionError",
        "message": "timed out connecting to civicrm.example.org",
    }
    assert "summary" not in rec
    assert "card_last4" not in rec


def test_card_last4_placement(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    diff = Diff(
        to_add=[_resolved(1, card_id=0x1234)],
        to_update_credential=[(_resolved(2, card_id=0x5678),
                               _unifi(2, card_id=0x1111))],
        to_update_policy=[(_resolved(3, card_id=0x9999),
                           _unifi(3, card_id=0x9999))],
        to_deactivate=[_unifi(4, card_id=0xABCD)],
        unmapped=[_resolved(5, card_id=0xFFFF)],
    )

    audit.log_applied(diff, dry_run=False, path=path, facility_code=42)

    rec = _read_lines(path)[0]
    # facility_code=42 => upper byte 0x2A => nfc_id = 0x2A0000 | card_id
    # last-4 hex of 0x2A0000|0x1234 = "1234"
    assert rec["card_last4"]["added"] == ["1234"]
    assert rec["card_last4"]["updated_credential"] == ["5678"]
    assert rec["card_last4"]["deactivated"] == ["ABCD"]
    # to_update_policy and unmapped are not included in card_last4
    assert set(rec["card_last4"].keys()) == {"added", "updated_credential", "deactivated"}


def test_two_writes_append_two_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    diff = Diff([], [], [], [], [])

    audit.log_applied(diff, dry_run=False, path=path, facility_code=42)
    audit.log_applied(diff, dry_run=True, path=path, facility_code=42)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["dry_run"] is False
    assert json.loads(lines[1])["dry_run"] is True


def test_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "audit.jsonl"
    diff = Diff([], [], [], [], [])

    audit.log_applied(diff, dry_run=False, path=path, facility_code=42)

    assert path.exists()


def test_redaction_canary_no_full_nfc_id_appears(tmp_path: Path) -> None:
    """Architecture §11: never log full card IDs at any level."""
    path = tmp_path / "audit.jsonl"
    diff = Diff(
        to_add=[_resolved(1, card_id=0x1234)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_unifi(2, card_id=0xABCD)],
        unmapped=[],
    )

    audit.log_applied(diff, dry_run=False, path=path, facility_code=42)

    raw = path.read_text(encoding="utf-8")
    # nfc_id full hex for facility 42 + card 0x1234 = "2A1234"
    assert "2A1234" not in raw
    # nfc_id full hex for facility 42 + card 0xABCD = "2AABCD"
    assert "2AABCD" not in raw
    # last-4 substrings DO appear
    assert "1234" in raw
    assert "ABCD" in raw
