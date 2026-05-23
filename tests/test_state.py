"""Tests for door_sync.state — persistent JSON state file."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from door_sync import state
from door_sync.models import State


def test_read_missing_file_returns_defaults(tmp_path: Path) -> None:
    result = state.read(tmp_path / "nope.json")
    assert result == State(None, None, None, 0)


def test_write_success_from_empty_sets_iso_and_increments_count(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    now = datetime(2026, 5, 23, 14, 32, 11, tzinfo=UTC)
    state.write_success(path, now=now)

    result = state.read(path)
    assert result == State(
        last_success_iso="2026-05-23T14:32:11Z",
        last_halt_iso=None,
        last_halt_reason=None,
        run_count=1,
    )


def test_write_success_preserves_existing_halt_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_halt(path, "earlier halt",
                     now=datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC))
    state.write_success(path,
                        now=datetime(2026, 5, 23, 11, 0, 0, tzinfo=UTC))

    result = state.read(path)
    assert result.last_success_iso == "2026-05-23T11:00:00Z"
    assert result.last_halt_iso == "2026-05-22T10:00:00Z"
    assert result.last_halt_reason == "earlier halt"
    assert result.run_count == 2


def test_write_halt_sets_halt_fields_and_preserves_success(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_success(path,
                        now=datetime(2026, 5, 20, 9, 0, 0, tzinfo=UTC))
    state.write_halt(path, "mass deactivate",
                     now=datetime(2026, 5, 23, 14, 0, 0, tzinfo=UTC))

    result = state.read(path)
    assert result.last_success_iso == "2026-05-20T09:00:00Z"
    assert result.last_halt_iso == "2026-05-23T14:00:00Z"
    assert result.last_halt_reason == "mass deactivate"
    assert result.run_count == 2


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_halt(path, "reason A",
                     now=datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC))
    state.write_success(path,
                        now=datetime(2026, 5, 23, 11, 0, 0, tzinfo=UTC))
    state.write_halt(path, "reason B",
                     now=datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC))

    result = state.read(path)
    assert result == State(
        last_success_iso="2026-05-23T11:00:00Z",
        last_halt_iso="2026-05-24T12:00:00Z",
        last_halt_reason="reason B",
        run_count=3,
    )


def test_atomic_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_success(path,
                        now=datetime(2026, 5, 23, 14, 0, 0, tzinfo=UTC))

    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_read_malformed_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        state.read(path)


def test_now_defaults_to_utc_iso_z(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_success(path)  # no now=

    result = state.read(path)
    assert result.last_success_iso is not None
    assert result.last_success_iso.endswith("Z")


def test_write_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "state.json"
    state.write_success(path,
                        now=datetime(2026, 5, 23, 14, 0, 0, tzinfo=UTC))

    assert path.exists()
