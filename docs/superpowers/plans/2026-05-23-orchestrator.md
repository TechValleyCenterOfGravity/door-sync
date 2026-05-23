# Orchestrator + ops stubs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire CiviCRM + UniFi clients + pure modules into `orchestrator.reconcile(config, *, dry_run)`, add minimal `audit` (JSONL file), `state` (JSON file), and `alert` (flag file + logger.error) modules, and expose everything through a `door-sync` CLI with `run --once`, `show-diff`, and `validate-config` subcommands.

**Architecture:** Strict layering per architecture §4 — `orchestrator` imports everything below it; `audit`/`state`/`alert` import only `models`; `__main__` imports `orchestrator`, `audit`, `alert`, `config`, `cli`. All side-effect modules use atomic-write patterns (tmp + `os.replace`) for files that matter. Tests use plain Python fake classes for client surfaces — no `unittest.mock` for the orchestrator integration tests.

**Tech Stack:** Python 3.13, `httpx` (sync), `argparse`, stdlib `logging`, `json`, `pytest`, `pytest-httpx` (already present), `mypy --strict`, `ruff`.

**Spec:** [`docs/superpowers/specs/2026-05-23-orchestrator-design.md`](../specs/2026-05-23-orchestrator-design.md)

---

## File Structure

```
src/door_sync/
├── models.py              # MODIFY — add `State` frozen dataclass
├── config.py              # MODIFY — add `OpsPaths`, embed in `Config`, validate `[ops]`
├── state.py               # CREATE — read/write_success/write_halt
├── audit.py               # CREATE — log_applied/log_halt/log_crashed (JSONL)
├── alert.py               # CREATE — raise_/clear (flag file + logger)
├── orchestrator.py        # CREATE — reconcile(config, *, dry_run)
├── cli.py                 # CREATE — pretty-printers for show-diff and validate-config
└── __main__.py            # REWRITE — argparse + cmd_run/cmd_show_diff/cmd_validate_config

tests/
├── test_state.py          # CREATE — 9 tests
├── test_audit.py          # CREATE — 8 tests
├── test_alert.py          # CREATE — 6 tests
├── test_orchestrator.py   # CREATE — 7 tests with inline FakeCivicrmClient/FakeUnifiClient
├── test_main.py           # CREATE — 7 tests via __main__.main(argv=[...])
└── test_config.py         # MODIFY — add tests for OpsPaths defaults + validation

config.example.toml        # MODIFY — add [ops] section
CLAUDE.md                  # MODIFY — replace Commands block + status line
docs/architecture.md       # MODIFY — §10 orchestrator code, §12 deferred items table
```

---

## Task 1: Add `State` frozen dataclass to models.py

**Files:**
- Modify: `src/door_sync/models.py`

**Why first:** Every other module that touches state.json needs this type. Trivially small change; commit alone so later tasks compile cleanly.

- [ ] **Step 1: Add the `State` dataclass at the end of `models.py`**

Append to `src/door_sync/models.py` (after `SafetyThresholds`):

```python
@dataclass(frozen=True)
class State:
    last_success_iso: str | None
    last_halt_iso: str | None
    last_halt_reason: str | None
    run_count: int
```

- [ ] **Step 2: Verify it type-checks and lints**

```bash
uv run mypy --strict src/door_sync/models.py
uv run ruff check src/door_sync/models.py
```

Expected: both clean.

- [ ] **Step 3: Commit**

```bash
git add src/door_sync/models.py
git commit -m "Add State dataclass for persistent run-state file"
```

---

## Task 2: Implement `state.py` (with TDD)

**Files:**
- Create: `src/door_sync/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Create `tests/test_state.py` with the read-missing-file test**

```python
"""Tests for door_sync.state — persistent JSON state file."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from door_sync import state
from door_sync.models import State


def test_read_missing_file_returns_defaults(tmp_path: Path) -> None:
    result = state.read(tmp_path / "nope.json")
    assert result == State(None, None, None, 0)
```

- [ ] **Step 2: Run; verify it fails**

```bash
uv run pytest tests/test_state.py -v
```

Expected: `ModuleNotFoundError: No module named 'door_sync.state'`.

- [ ] **Step 3: Create the minimal `state.py` to make it pass**

Create `src/door_sync/state.py`:

```python
"""Persistent state file for door-sync.

Records the last successful reconciliation, the last halt, and a
monotonically increasing run counter. Read by external health-check
scripts; written by the orchestrator on success or halt (never on crash,
never on dry-run — see spec §7).

Atomic write via tmp + os.replace: a torn write cannot leave a
half-written file. Reading a malformed JSON file raises, so an operator
sees the corruption explicitly rather than silently losing history.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from door_sync.models import State


def read(path: Path) -> State:
    if not path.exists():
        return State(None, None, None, 0)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return State(
        last_success_iso=data.get("last_success_iso"),
        last_halt_iso=data.get("last_halt_iso"),
        last_halt_reason=data.get("last_halt_reason"),
        run_count=int(data.get("run_count", 0)),
    )
```

- [ ] **Step 4: Run; verify the test passes**

```bash
uv run pytest tests/test_state.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Add `write_success` test + iso-Z + run_count increment expectations**

Append to `tests/test_state.py`:

```python
def test_write_success_from_empty_sets_iso_and_increments_count(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    now = datetime(2026, 5, 23, 14, 32, 11, tzinfo=timezone.utc)
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
                     now=datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc))
    state.write_success(path,
                        now=datetime(2026, 5, 23, 11, 0, 0, tzinfo=timezone.utc))

    result = state.read(path)
    assert result.last_success_iso == "2026-05-23T11:00:00Z"
    assert result.last_halt_iso == "2026-05-22T10:00:00Z"
    assert result.last_halt_reason == "earlier halt"
    assert result.run_count == 2
```

- [ ] **Step 6: Run; verify both fail (no `write_success` / `write_halt`)**

```bash
uv run pytest tests/test_state.py -v
```

Expected: 2 fail with `AttributeError`.

- [ ] **Step 7: Add `write_success`, `write_halt`, helpers to `state.py`**

Append to `src/door_sync/state.py`:

```python
def write_success(path: Path, *, now: datetime | None = None) -> None:
    current = read(path) if path.exists() else State(None, None, None, 0)
    when = now if now is not None else datetime.now(timezone.utc)
    new = State(
        last_success_iso=_iso_z(when),
        last_halt_iso=current.last_halt_iso,
        last_halt_reason=current.last_halt_reason,
        run_count=current.run_count + 1,
    )
    _atomic_write(path, new)


def write_halt(path: Path, reason: str, *, now: datetime | None = None) -> None:
    current = read(path) if path.exists() else State(None, None, None, 0)
    when = now if now is not None else datetime.now(timezone.utc)
    new = State(
        last_success_iso=current.last_success_iso,
        last_halt_iso=_iso_z(when),
        last_halt_reason=reason,
        run_count=current.run_count + 1,
    )
    _atomic_write(path, new)


def _iso_z(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, payload: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = {
        "last_success_iso": payload.last_success_iso,
        "last_halt_iso": payload.last_halt_iso,
        "last_halt_reason": payload.last_halt_reason,
        "run_count": payload.run_count,
    }
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
```

- [ ] **Step 8: Run; verify all 3 tests pass**

```bash
uv run pytest tests/test_state.py -v
```

Expected: 3 passed.

- [ ] **Step 9: Add remaining tests (halt path, round-trip, atomic canary, malformed, now-default, parent-dir creation)**

Append to `tests/test_state.py`:

```python
def test_write_halt_sets_halt_fields_and_preserves_success(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_success(path,
                        now=datetime(2026, 5, 20, 9, 0, 0, tzinfo=timezone.utc))
    state.write_halt(path, "mass deactivate",
                     now=datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc))

    result = state.read(path)
    assert result.last_success_iso == "2026-05-20T09:00:00Z"
    assert result.last_halt_iso == "2026-05-23T14:00:00Z"
    assert result.last_halt_reason == "mass deactivate"
    assert result.run_count == 2


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_halt(path, "reason A",
                     now=datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc))
    state.write_success(path,
                        now=datetime(2026, 5, 23, 11, 0, 0, tzinfo=timezone.utc))
    state.write_halt(path, "reason B",
                     now=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc))

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
                        now=datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc))

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
                        now=datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc))

    assert path.exists()
```

Also add `import json` at the top if not present (already imported via the first test? No — add it explicitly).

Final imports for `tests/test_state.py` should be:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from door_sync import state
from door_sync.models import State
```

- [ ] **Step 10: Run full state test file; verify all 9 pass**

```bash
uv run pytest tests/test_state.py -v
```

Expected: 9 passed.

- [ ] **Step 11: Type-check and lint**

```bash
uv run mypy --strict src/door_sync/state.py tests/test_state.py
uv run ruff check src/door_sync/state.py tests/test_state.py
```

Expected: both clean.

- [ ] **Step 12: Commit**

```bash
git add src/door_sync/state.py tests/test_state.py
git commit -m "Add state.py: atomic JSON persistence for run state"
```

---

## Task 3: Implement `audit.py` (with TDD)

**Files:**
- Create: `src/door_sync/audit.py`
- Test: `tests/test_audit.py`

Audit's per-record schema is in spec §6. `nfc_id` is computed as `(facility_code << 16) | card_id`; last-4 chars of its uppercase hex are recorded.

- [ ] **Step 1: Create `tests/test_audit.py` with the `log_applied` happy-path test**

```python
"""Tests for door_sync.audit — append-only JSONL audit log."""

import json
from pathlib import Path

import pytest

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


def _read_lines(path: Path) -> list[dict[str, object]]:
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
```

- [ ] **Step 2: Run; verify it fails**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: `ModuleNotFoundError: No module named 'door_sync.audit'`.

- [ ] **Step 3: Create minimal `audit.py`**

Create `src/door_sync/audit.py`:

```python
"""Append-only JSONL audit log for door-sync.

One JSON object per line, one line per cycle outcome. Compatible with
logrotate copytruncate (open-append per write; no long-lived handle).
Card IDs are recorded as the last 4 chars of nfc_id hex, never as the
raw integer or full hex, per architecture §11.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from door_sync.models import Diff


def log_applied(
    diff: Diff,
    *,
    dry_run: bool,
    path: Path,
    facility_code: int,
) -> None:
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": "applied",
        "dry_run": dry_run,
        "summary": _summary(diff),
        "card_last4": _card_last4(diff, facility_code),
    }
    _append(path, record)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(diff: Diff) -> dict[str, int]:
    return {
        "added": len(diff.to_add),
        "updated_credential": len(diff.to_update_credential),
        "updated_policy": len(diff.to_update_policy),
        "deactivated": len(diff.to_deactivate),
        "unmapped": len(diff.unmapped),
    }


def _card_last4(diff: Diff, facility_code: int) -> dict[str, list[str]]:
    return {
        "added": [
            _last4_nfc(m.card_id, facility_code)
            for m in diff.to_add
            if m.card_id is not None
        ],
        "updated_credential": [
            _last4_nfc(m.card_id, facility_code)
            for m, _ in diff.to_update_credential
            if m.card_id is not None
        ],
        "deactivated": [
            _last4_nfc(u.card_id, facility_code)
            for u in diff.to_deactivate
            if u.card_id is not None
        ],
    }


def _last4_nfc(card_id: int, facility_code: int) -> str:
    nfc_id = (facility_code << 16) | card_id
    nfc_hex = format(nfc_id, "X")
    return nfc_hex[-4:] if len(nfc_hex) >= 4 else nfc_hex.rjust(4, "0")


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
```

- [ ] **Step 4: Run; verify the test passes**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Add dry-run + halt + crashed tests**

Append to `tests/test_audit.py`:

```python
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
```

- [ ] **Step 6: Run; verify the new tests fail (log_halt/log_crashed missing)**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: 3 fail with `AttributeError`.

- [ ] **Step 7: Add `log_halt` and `log_crashed` to `audit.py`**

Insert into `src/door_sync/audit.py` after `log_applied`:

```python
def log_halt(
    reason: str,
    diff: Diff,
    *,
    dry_run: bool,
    path: Path,
    facility_code: int,
) -> None:
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": "halted",
        "dry_run": dry_run,
        "reason": reason,
        "summary": _summary(diff),
        "card_last4": _card_last4(diff, facility_code),
    }
    _append(path, record)


def log_crashed(exc: BaseException, *, path: Path) -> None:
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": "crashed",
        "dry_run": False,
        "exception": {
            "class": type(exc).__name__,
            "message": str(exc),
        },
    }
    _append(path, record)
```

- [ ] **Step 8: Run; verify all 4 pass**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: 4 passed.

- [ ] **Step 9: Add card-last4 placement test, append test, missing-parent test, redaction canary**

Append to `tests/test_audit.py`:

```python
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
```

- [ ] **Step 10: Run all 8; verify they pass**

```bash
uv run pytest tests/test_audit.py -v
```

Expected: 8 passed.

- [ ] **Step 11: Type-check and lint**

```bash
uv run mypy --strict src/door_sync/audit.py tests/test_audit.py
uv run ruff check src/door_sync/audit.py tests/test_audit.py
```

Expected: both clean.

- [ ] **Step 12: Commit**

```bash
git add src/door_sync/audit.py tests/test_audit.py
git commit -m "Add audit.py: append-only JSONL log with nfc_id last-4 redaction"
```

---

## Task 4: Implement `alert.py` (with TDD)

**Files:**
- Create: `src/door_sync/alert.py`
- Test: `tests/test_alert.py`

- [ ] **Step 1: Create `tests/test_alert.py` with all 6 tests**

```python
"""Tests for door_sync.alert — flag-file alerting stub."""

import logging
from pathlib import Path

import pytest

from door_sync import alert


def test_raise_creates_file_with_reason(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("mass_deactivate exceeded threshold", path=path)

    assert path.exists()
    assert path.read_text(encoding="utf-8") == "mass_deactivate exceeded threshold\n"


def test_raise_overwrites_previous_reason(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("first reason", path=path)
    alert.raise_("second reason", path=path)

    assert path.read_text(encoding="utf-8") == "second reason\n"


def test_clear_removes_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("a reason", path=path)
    assert path.exists()

    alert.clear(path=path)
    assert not path.exists()


def test_clear_missing_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "never_existed.flag"

    alert.clear(path=path)  # should not raise
    assert not path.exists()


def test_raise_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "alert.flag"
    alert.raise_("reason", path=path)

    assert path.exists()


def test_raise_logs_error_with_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "alert.flag"

    with caplog.at_level(logging.ERROR, logger="door_sync.alert"):
        alert.raise_("the reason", path=path)

    assert any(
        record.levelno == logging.ERROR and "the reason" in record.getMessage()
        for record in caplog.records
    )
```

- [ ] **Step 2: Run; verify all fail (no module)**

```bash
uv run pytest tests/test_alert.py -v
```

Expected: 6 errors, `ModuleNotFoundError`.

- [ ] **Step 3: Create `src/door_sync/alert.py`**

```python
"""Flag-file alerting stub for door-sync.

Two operations: raise_ (create/overwrite flag file with reason, log ERROR)
and clear (remove flag file). Presence of the flag file = alert active;
external monitoring (Nagios, Prometheus textfile collector, etc.) can
detect halts without parsing logs. SMTP/webhook transport is deferred
per architecture §12.
"""

import logging
import os
from pathlib import Path

_logger = logging.getLogger("door_sync.alert")


def raise_(reason: str, *, path: Path) -> None:
    _logger.error("ALERT: %s", reason)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(reason + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def clear(*, path: Path) -> None:
    path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run; verify all 6 pass**

```bash
uv run pytest tests/test_alert.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Type-check and lint**

```bash
uv run mypy --strict src/door_sync/alert.py tests/test_alert.py
uv run ruff check src/door_sync/alert.py tests/test_alert.py
```

Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add src/door_sync/alert.py tests/test_alert.py
git commit -m "Add alert.py: flag-file + logger.error alerting stub"
```

---

## Task 5: Add `OpsPaths` to config.py + extend example TOML

**Files:**
- Modify: `src/door_sync/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Add tests for `OpsPaths` defaults and validation in `tests/test_config.py`**

Find the existing valid-config helper (search `tests/test_config.py` for a function that builds a valid TOML string — typically `_valid_toml` or similar). If none exists, the new tests below construct their own minimal config.

Open `tests/test_config.py` and append:

```python
def test_ops_paths_default_when_section_omitted(tmp_path: Path) -> None:
    """If [ops] is missing entirely, defaults from architecture §11 apply."""
    cfg_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    cfg_path.write_text(_minimal_valid_toml(), encoding="utf-8")
    env_path.write_text("CIVICRM_API_KEY=k\nUNIFI_API_KEY=k\n", encoding="utf-8")

    config = config_mod.load(config_path=cfg_path, env_path=env_path)

    assert config.ops_paths.audit_jsonl == Path("/var/log/door-sync/audit.jsonl")
    assert config.ops_paths.state_json == Path("/var/lib/door-sync/state.json")
    assert config.ops_paths.alert_flag == Path("/var/run/door-sync/alert.flag")


def test_ops_paths_explicit_values_override_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    cfg_path.write_text(
        _minimal_valid_toml() + (
            "\n[ops]\n"
            'audit_jsonl = "/tmp/a.jsonl"\n'
            'state_json  = "/tmp/s.json"\n'
            'alert_flag  = "/tmp/f.flag"\n'
        ),
        encoding="utf-8",
    )
    env_path.write_text("CIVICRM_API_KEY=k\nUNIFI_API_KEY=k\n", encoding="utf-8")

    config = config_mod.load(config_path=cfg_path, env_path=env_path)

    assert config.ops_paths.audit_jsonl == Path("/tmp/a.jsonl")
    assert config.ops_paths.state_json == Path("/tmp/s.json")
    assert config.ops_paths.alert_flag == Path("/tmp/f.flag")


def test_ops_paths_rejects_non_string_value(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    cfg_path.write_text(
        _minimal_valid_toml() + (
            "\n[ops]\n"
            "audit_jsonl = 42\n"
        ),
        encoding="utf-8",
    )
    env_path.write_text("CIVICRM_API_KEY=k\nUNIFI_API_KEY=k\n", encoding="utf-8")

    with pytest.raises(config_mod.ConfigError) as excinfo:
        config_mod.load(config_path=cfg_path, env_path=env_path)

    paths = [issue.path for issue in excinfo.value.issues]
    assert "ops.audit_jsonl" in paths
```

Then add a `_minimal_valid_toml()` helper near the top of the file (if there isn't one already; check first):

```python
def _minimal_valid_toml() -> str:
    """Smallest valid config.toml — just enough to pass _validate_*."""
    return (
        "cadence_seconds = 600\n"
        "[civicrm]\n"
        'host = "https://civicrm.example.org"\n'
        'card_id_field = "Door_Access.card_id"\n'
        "[unifi]\n"
        'host = "https://unifi.example.org:12445"\n'
        'tls_fingerprint = "'
        + ("AB:" * 31) + 'AB"\n'
        "facility_code = 42\n"
        "[safety]\n"
        "[tier_mapping.rules.Gold]\n"
        'resolution = "tier"\n'
        'target_policy = "p1"\n'
        "rank = 100\n"
    )
```

If `tests/test_config.py` already has a similar helper, reuse it instead of duplicating.

Required imports at the top of `tests/test_config.py` (add any missing):

```python
from pathlib import Path

import pytest

from door_sync import config as config_mod
```

- [ ] **Step 2: Run; verify all 3 new tests fail (no `ops_paths` attribute)**

```bash
uv run pytest tests/test_config.py -v -k ops_paths
```

Expected: 3 fail with `AttributeError` or `ConfigError` about unknown structure.

- [ ] **Step 3: Add `OpsPaths` dataclass and embed in `Config`**

In `src/door_sync/config.py`, add after the `UnifiConfig` definition (around line 42):

```python
@dataclass(frozen=True)
class OpsPaths:
    audit_jsonl: Path
    state_json: Path
    alert_flag: Path
```

Update the existing `Config` dataclass (around line 45) to:

```python
@dataclass(frozen=True)
class Config:
    cadence_seconds: int
    civicrm: CivicrmConfig
    unifi: UnifiConfig
    safety: SafetyThresholds
    tier_mapping: TierMapping
    ops_paths: OpsPaths
```

- [ ] **Step 4: Add `_validate_ops` helper near other validators**

Add to `src/door_sync/config.py` (place near the other `_validate_*` helpers, e.g. after `_validate_safety`):

```python
_DEFAULT_OPS_PATHS = OpsPaths(
    audit_jsonl=Path("/var/log/door-sync/audit.jsonl"),
    state_json=Path("/var/lib/door-sync/state.json"),
    alert_flag=Path("/var/run/door-sync/alert.flag"),
)


def _validate_ops(
    data: dict[str, Any], issues: list[ConfigIssue]
) -> OpsPaths:
    section = data.get("ops", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="ops", message="must be a table"))
        return _DEFAULT_OPS_PATHS

    def _string_path(key: str, default: Path) -> Path:
        raw = section.get(key)
        if raw is None:
            return default
        if not isinstance(raw, str):
            issues.append(
                ConfigIssue(
                    path=f"ops.{key}",
                    message=f"must be string, got {type(raw).__name__}",
                )
            )
            return default
        return Path(raw)

    return OpsPaths(
        audit_jsonl=_string_path("audit_jsonl", _DEFAULT_OPS_PATHS.audit_jsonl),
        state_json=_string_path("state_json", _DEFAULT_OPS_PATHS.state_json),
        alert_flag=_string_path("alert_flag", _DEFAULT_OPS_PATHS.alert_flag),
    )
```

- [ ] **Step 5: Wire `_validate_ops` into `load`**

In `src/door_sync/config.py`, find the `load` function (around line 121). Update it to call `_validate_ops` and pass the result to the `Config(...)` constructor:

```python
def load(
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """Load and validate config from TOML + env. See module docstring for details."""
    config_path, env_path = _resolve_paths(config_path, env_path)
    issues: list[ConfigIssue] = []

    try:
        with config_path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
    except FileNotFoundError as exc:
        issues.append(
            ConfigIssue(path="config_file", message=f"file not found: {config_path}")
        )
        raise ConfigError(issues) from exc
    except tomllib.TOMLDecodeError as e:
        issues.append(ConfigIssue(path="config_file", message=f"invalid TOML: {e}"))
        raise ConfigError(issues) from e

    file_env: dict[str, str] = {}
    try:
        file_env = _load_env_file(env_path)
    except ValueError as e:
        issues.append(ConfigIssue(path="env_file", message=str(e)))

    def env_get(name: str) -> str | None:
        val = file_env.get(name)
        return val if val is not None else os.environ.get(name)

    cadence = _validate_cadence(data, issues)
    civicrm = _validate_civicrm(data, issues, env_get)
    unifi = _validate_unifi(data, issues, env_get)
    safety = _validate_safety(data, issues)
    tier_mapping = _validate_tier_mapping(data, issues)
    ops_paths = _validate_ops(data, issues)

    if issues:
        raise ConfigError(issues)

    return Config(
        cadence_seconds=cadence,
        civicrm=civicrm,
        unifi=unifi,
        safety=safety,
        tier_mapping=tier_mapping,
        ops_paths=ops_paths,
    )
```

- [ ] **Step 6: Run new tests; verify they pass**

```bash
uv run pytest tests/test_config.py -v -k ops_paths
```

Expected: 3 passed.

- [ ] **Step 7: Run full config test suite to catch regressions**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all pass. If any previously-passing tests now fail because they construct `Config(...)` directly, update their constructor calls to include `ops_paths=_DEFAULT_OPS_PATHS` (or import the helper).

If `tests/test_config.py` builds a `Config` directly anywhere (search for `Config(` calls), add a default `ops_paths` arg. Hint: easiest is to import `_DEFAULT_OPS_PATHS` from `door_sync.config` for the test only.

- [ ] **Step 8: Update `config.example.toml`**

Append to `config.example.toml`:

```toml

[ops]
# Operational file paths. All three are optional; defaults shown.
# - audit_jsonl: append-only JSONL of every cycle's outcome (logrotate-friendly).
# - state_json: persistent last-success/last-halt for healthchecks.
# - alert_flag: written on halt, removed on success; for external monitoring.
audit_jsonl = "/var/log/door-sync/audit.jsonl"
state_json  = "/var/lib/door-sync/state.json"
alert_flag  = "/var/run/door-sync/alert.flag"
```

- [ ] **Step 9: Type-check and lint**

```bash
uv run mypy --strict src tests
uv run ruff check .
```

Expected: both clean.

- [ ] **Step 10: Commit**

```bash
git add src/door_sync/config.py tests/test_config.py config.example.toml
git commit -m "Add OpsPaths to Config with [ops] TOML section"
```

---

## Task 6: Implement `orchestrator.py` (with TDD)

**Files:**
- Create: `src/door_sync/orchestrator.py`
- Test: `tests/test_orchestrator.py`

This is the integration seam. Tests use plain Python fake classes (not `unittest.mock`) so the test file documents the exact client surface the orchestrator depends on.

- [ ] **Step 1: Create `tests/test_orchestrator.py` with fakes + happy path**

```python
"""Tests for door_sync.orchestrator — full reconcile cycle integration.

Uses inline FakeCivicrmClient and FakeUnifiClient (plain Python classes
matching the real clients' surface). No unittest.mock — the fakes
document what orchestrator.reconcile actually depends on.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from door_sync import orchestrator
from door_sync.config import (
    CivicrmConfig,
    Config,
    OpsPaths,
    UnifiConfig,
)
from door_sync.models import (
    CiviMember,
    Diff,
    ReconcileResult,
    ResolvedMember,
    SafetyThresholds,
    TierMapping,
    TierRule,
    UnifiUser,
)


class FakeCivicrmClient:
    """Matches CivicrmClient surface: fetch_active() + context manager."""

    def __init__(
        self,
        cfg: CivicrmConfig,
        *,
        members: list[CiviMember] | None = None,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._members = members or []
        self._raise = raise_on_fetch

    def fetch_active(self) -> list[CiviMember]:
        if self._raise is not None:
            raise self._raise
        return self._members

    def __enter__(self) -> "FakeCivicrmClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class FakeUnifiClient:
    """Matches UnifiClient surface: fetch_users(), apply(diff), dry_run flag."""

    def __init__(
        self,
        cfg: UnifiConfig,
        *,
        dry_run: bool = False,
        users: list[UnifiUser] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self._users = list(users or [])
        self.apply_calls: list[Diff] = []

    def fetch_users(self) -> list[UnifiUser]:
        return list(self._users)

    def apply(self, diff: Diff) -> None:
        self.apply_calls.append(diff)
        if self.dry_run:
            return
        # Mutate the in-memory store so a follow-up fetch reflects the writes
        # (used by the idempotency canary test).
        by_contact = {u.contact_id: u for u in self._users}
        for m in diff.to_add:
            by_contact[m.contact_id] = UnifiUser(
                contact_id=m.contact_id,
                display_name=m.display_name,
                card_id=m.card_id,
                active=True,
                policy=m.target_policy,
            )
        for m, _ in diff.to_update_credential:
            existing = by_contact[m.contact_id]
            by_contact[m.contact_id] = UnifiUser(
                contact_id=existing.contact_id,
                display_name=existing.display_name,
                card_id=m.card_id,
                active=existing.active,
                policy=existing.policy,
            )
        for m, _ in diff.to_update_policy:
            existing = by_contact[m.contact_id]
            by_contact[m.contact_id] = UnifiUser(
                contact_id=existing.contact_id,
                display_name=existing.display_name,
                card_id=existing.card_id,
                active=existing.active,
                policy=m.target_policy,
            )
        for u in diff.to_deactivate:
            existing = by_contact[u.contact_id]
            by_contact[u.contact_id] = UnifiUser(
                contact_id=existing.contact_id,
                display_name=existing.display_name,
                card_id=existing.card_id,
                active=False,
                policy=existing.policy,
            )
        self._users = list(by_contact.values())

    def __enter__(self) -> "FakeUnifiClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _config(tmp_path: Path) -> Config:
    return Config(
        cadence_seconds=600,
        civicrm=CivicrmConfig(
            host="https://civicrm.example.org",
            api_key="k",
            card_id_field="Door_Access.card_id",
        ),
        unifi=UnifiConfig(
            host="https://unifi.example.org:12445",
            api_key="k",
            tls_fingerprint="AB:" * 31 + "AB",
            facility_code=42,
        ),
        safety=SafetyThresholds(),
        tier_mapping=TierMapping(
            rules={"Gold": TierRule(resolution="tier", target_policy="p1", rank=100)}
        ),
        ops_paths=OpsPaths(
            audit_jsonl=tmp_path / "audit.jsonl",
            state_json=tmp_path / "state.json",
            alert_flag=tmp_path / "alert.flag",
        ),
    )


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    civi_members: list[CiviMember],
    unifi_users: list[UnifiUser],
    civi_raise: Exception | None = None,
) -> dict[str, Any]:
    """Patch CivicrmClient and UnifiClient symbols in orchestrator namespace."""
    holder: dict[str, Any] = {}

    def make_civi(cfg: CivicrmConfig) -> FakeCivicrmClient:
        client = FakeCivicrmClient(cfg, members=civi_members, raise_on_fetch=civi_raise)
        holder["civi"] = client
        return client

    def make_unifi(cfg: UnifiConfig, *, dry_run: bool = False) -> FakeUnifiClient:
        client = FakeUnifiClient(cfg, dry_run=dry_run, users=unifi_users)
        holder["unifi"] = client
        return client

    monkeypatch.setattr(orchestrator, "CivicrmClient", make_civi)
    monkeypatch.setattr(orchestrator, "UnifiClient", make_unifi)
    return holder


def test_happy_path_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    # 12 baseline active users (above SafetyThresholds.baseline_floor=10)
    members = [
        CiviMember(contact_id=i, display_name=f"User {i}",
                   card_id=0x1000 + i, membership_types=["Gold"])
        for i in range(1, 13)
    ]
    users = [
        UnifiUser(contact_id=i, display_name=f"User {i}",
                  card_id=0x1000 + i, active=True, policy="p1")
        for i in range(1, 13)
    ]
    _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is False
    assert result.diff is not None
    assert result.diff.to_add == []
    assert result.diff.to_deactivate == []

    # Audit: one applied line
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["event"] == "applied"

    # State: last_success_iso populated; alert flag absent
    assert (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()
```

- [ ] **Step 2: Run; verify it fails (no orchestrator module)**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: `ModuleNotFoundError: No module named 'door_sync.orchestrator'`.

- [ ] **Step 3: Create `src/door_sync/orchestrator.py`**

```python
"""Single reconcile entry point. Wires CiviCRM + UniFi clients + pure
modules + audit + alert + state per architecture §10.

Invariants:
  - No globals; everything comes from `config`.
  - Clients are constructed per cycle (cheap; gives clean isolation).
  - Pure modules behave identically in dry-run and live.
  - Exceptions propagate — this function does not catch. __main__ does.
"""

from door_sync import alert, audit, reconciler, safety, state, tier_mapping
from door_sync.civicrm.client import CivicrmClient
from door_sync.config import Config
from door_sync.models import ReconcileResult
from door_sync.unifi.client import UnifiClient


def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:
    civicrm = CivicrmClient(config.civicrm)
    unifi = UnifiClient(config.unifi, dry_run=dry_run)
    paths = config.ops_paths

    civi_members = civicrm.fetch_active()
    resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
    unifi_users = unifi.fetch_users()

    diff = reconciler.compute_diff(resolved, unifi_users)
    active_baseline = sum(1 for u in unifi_users if u.active)
    check = safety.check(diff, baseline=active_baseline, thresholds=config.safety)

    if check.halted:
        audit.log_halt(
            check.reason or "",
            diff,
            dry_run=dry_run,
            path=paths.audit_jsonl,
            facility_code=config.unifi.facility_code,
        )
        alert.raise_(check.reason or "halted", path=paths.alert_flag)
        if not dry_run:
            state.write_halt(paths.state_json, check.reason or "")
        return ReconcileResult(halted=True, reason=check.reason, diff=diff)

    unifi.apply(diff)
    audit.log_applied(
        diff,
        dry_run=dry_run,
        path=paths.audit_jsonl,
        facility_code=config.unifi.facility_code,
    )
    if not dry_run:
        state.write_success(paths.state_json)
        alert.clear(path=paths.alert_flag)
    return ReconcileResult(halted=False, reason=None, diff=diff)
```

(Note: `check.reason or ""` defensively coerces `None` since `CheckResult.reason: str | None`; in practice `halted=True` always carries a reason, but the type system requires the fallback.)

- [ ] **Step 4: Run happy-path test; verify it passes**

```bash
uv run pytest tests/test_orchestrator.py::test_happy_path_no_drift -v
```

Expected: 1 passed.

- [ ] **Step 5: Add apply-drift, halt, idempotency, dry-run apply, dry-run halt, exception tests**

Append to `tests/test_orchestrator.py`:

```python
def test_apply_with_drift_calls_unifi_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    members = [
        CiviMember(contact_id=i, display_name=f"User {i}",
                   card_id=0x1000 + i, membership_types=["Gold"])
        for i in range(1, 12)
    ]
    members.append(  # new member not yet in UniFi
        CiviMember(contact_id=99, display_name="New",
                   card_id=0x9999, membership_types=["Gold"])
    )
    users = [
        UnifiUser(contact_id=i, display_name=f"User {i}",
                  card_id=0x1000 + i, active=True, policy="p1")
        for i in range(1, 12)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is False
    unifi: FakeUnifiClient = holder["unifi"]
    assert len(unifi.apply_calls) == 1
    assert len(unifi.apply_calls[0].to_add) == 1
    assert unifi.apply_calls[0].to_add[0].contact_id == 99


def test_safety_halt_writes_alert_flag_and_audit_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    # 20 users in UniFi; CiviCRM returns only 10 — would deactivate 50% > 15% threshold
    members = [
        CiviMember(contact_id=i, display_name=f"User {i}",
                   card_id=0x1000 + i, membership_types=["Gold"])
        for i in range(1, 11)
    ]
    users = [
        UnifiUser(contact_id=i, display_name=f"User {i}",
                  card_id=0x1000 + i, active=True, policy="p1")
        for i in range(1, 21)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is True
    assert result.reason is not None
    unifi: FakeUnifiClient = holder["unifi"]
    assert unifi.apply_calls == []  # never applied

    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    halted = json.loads(audit_lines[0])
    assert halted["event"] == "halted"
    assert halted["reason"] == result.reason

    flag = tmp_path / "alert.flag"
    assert flag.exists()
    assert result.reason in flag.read_text()


def test_idempotency_canary_second_cycle_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After applying drift, the next compute_diff (with the same data) must
    yield empty diff sets — architecture §8."""
    cfg = _config(tmp_path)
    members = [
        CiviMember(contact_id=i, display_name=f"User {i}",
                   card_id=0x1000 + i, membership_types=["Gold"])
        for i in range(1, 13)
    ]
    members.append(
        CiviMember(contact_id=99, display_name="New",
                   card_id=0x9999, membership_types=["Gold"])
    )
    users = [
        UnifiUser(contact_id=i, display_name=f"User {i}",
                  card_id=0x1000 + i, active=True, policy="p1")
        for i in range(1, 13)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    # First cycle: apply
    first = orchestrator.reconcile(cfg, dry_run=False)
    assert first.halted is False
    assert first.diff is not None
    assert len(first.diff.to_add) == 1

    # Second cycle: the same FakeUnifiClient (held in monkeypatched factory)
    # should now have the new user in its store, so diff is empty.
    second = orchestrator.reconcile(cfg, dry_run=False)
    assert second.halted is False
    assert second.diff is not None
    assert second.diff.to_add == []
    assert second.diff.to_update_credential == []
    assert second.diff.to_update_policy == []
    assert second.diff.to_deactivate == []


def test_dry_run_apply_does_not_touch_state_or_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    members = [
        CiviMember(contact_id=i, display_name=f"User {i}",
                   card_id=0x1000 + i, membership_types=["Gold"])
        for i in range(1, 13)
    ]
    members.append(
        CiviMember(contact_id=99, display_name="New",
                   card_id=0x9999, membership_types=["Gold"])
    )
    users = [
        UnifiUser(contact_id=i, display_name=f"User {i}",
                  card_id=0x1000 + i, active=True, policy="p1")
        for i in range(1, 13)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=True)

    assert result.halted is False
    unifi: FakeUnifiClient = holder["unifi"]
    assert unifi.dry_run is True
    # apply was still called; dry_run guarding lives inside UnifiClient
    assert len(unifi.apply_calls) == 1

    audit_line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit_line["dry_run"] is True

    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()


def test_dry_run_halt_writes_alert_flag_but_not_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    members = [
        CiviMember(contact_id=i, display_name=f"User {i}",
                   card_id=0x1000 + i, membership_types=["Gold"])
        for i in range(1, 11)
    ]
    users = [
        UnifiUser(contact_id=i, display_name=f"User {i}",
                  card_id=0x1000 + i, active=True, policy="p1")
        for i in range(1, 21)
    ]
    _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=True)

    assert result.halted is True

    audit_line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit_line["event"] == "halted"
    assert audit_line["dry_run"] is True

    assert (tmp_path / "alert.flag").exists()  # halt still flags
    assert not (tmp_path / "state.json").exists()  # but dry-run never touches state


def test_civicrm_exception_propagates_with_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    _patch_clients(
        monkeypatch,
        civi_members=[],
        unifi_users=[],
        civi_raise=ConnectionError("DNS lookup failed"),
    )

    with pytest.raises(ConnectionError):
        orchestrator.reconcile(cfg, dry_run=False)

    # Orchestrator does not catch — so no audit/state/alert touched by it.
    assert not (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()
```

- [ ] **Step 6: Run all 7 orchestrator tests**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: 7 passed. If any fail, the most likely cause is a real-client constructor signature mismatch — check `CivicrmClient.__init__` and `UnifiClient.__init__` in their respective files and align the fakes.

- [ ] **Step 7: Type-check and lint**

```bash
uv run mypy --strict src/door_sync/orchestrator.py tests/test_orchestrator.py
uv run ruff check src/door_sync/orchestrator.py tests/test_orchestrator.py
```

Expected: both clean.

- [ ] **Step 8: Run the entire suite to confirm no regressions**

```bash
uv run pytest
```

Expected: everything green.

- [ ] **Step 9: Commit**

```bash
git add src/door_sync/orchestrator.py tests/test_orchestrator.py
git commit -m "Add orchestrator.reconcile wiring clients + pure modules + audit/alert/state"
```

---

## Task 7: Implement `cli.py` (pretty-printers)

**Files:**
- Create: `src/door_sync/cli.py`
- Test: `tests/test_cli.py` (small)

The CLI helpers print diffs and config issues. Kept separate from `__main__.py` so they're testable without subprocess.

- [ ] **Step 1: Create `tests/test_cli.py` with print_diff + print_config_issues smoke tests**

```python
"""Tests for door_sync.cli — pretty-printers for show-diff and validate-config."""

import io
from pathlib import Path

from door_sync import cli
from door_sync.config import ConfigIssue
from door_sync.models import Diff, ResolvedMember, UnifiUser


def test_print_diff_renders_five_sections() -> None:
    diff = Diff(
        to_add=[ResolvedMember(1, "Alice", 0x1234, "p1", "tier")],
        to_update_credential=[
            (ResolvedMember(2, "Bob", 0x5678, "p1", "tier"),
             UnifiUser(2, "Bob", 0x1111, True, "p1")),
        ],
        to_update_policy=[
            (ResolvedMember(3, "Carol", 0x9999, "p2", "tier"),
             UnifiUser(3, "Carol", 0x9999, True, "p1")),
        ],
        to_deactivate=[UnifiUser(4, "Dave", 0xABCD, True, "p1")],
        unmapped=[ResolvedMember(5, "Eve", 0xFFFF, None, "unmapped")],
    )
    out = io.StringIO()

    cli.print_diff(diff, file=out)

    text = out.getvalue()
    assert "=== ADD (1) ===" in text
    assert "Alice" in text
    assert "=== UPDATE CREDENTIAL (1) ===" in text
    assert "Bob" in text
    assert "=== UPDATE POLICY (1) ===" in text
    assert "Carol" in text
    assert "=== DEACTIVATE (1) ===" in text
    assert "Dave" in text
    assert "=== UNMAPPED (1) ===" in text
    assert "Eve" in text


def test_print_diff_empty_sections_still_print_header() -> None:
    diff = Diff([], [], [], [], [])
    out = io.StringIO()

    cli.print_diff(diff, file=out)

    text = out.getvalue()
    assert "=== ADD (0) ===" in text
    assert "=== UNMAPPED (0) ===" in text


def test_print_config_issues_one_line_per_issue() -> None:
    issues = [
        ConfigIssue(path="civicrm.host", message="must start with https://"),
        ConfigIssue(path="UNIFI_API_KEY", message="required env var is missing or empty"),
    ]
    out = io.StringIO()

    cli.print_config_issues(issues, file=out)

    text = out.getvalue()
    assert "civicrm.host: must start with https://" in text
    assert "UNIFI_API_KEY: required env var is missing or empty" in text
```

- [ ] **Step 2: Run; verify it fails (no module)**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `src/door_sync/cli.py`**

```python
"""CLI pretty-printers used by __main__'s show-diff and validate-config.

Kept separate from __main__.py so they're unit-testable without subprocess.
"""

from typing import IO

from door_sync.config import ConfigIssue
from door_sync.models import Diff, ResolvedMember, UnifiUser


def print_diff(diff: Diff, *, file: IO[str]) -> None:
    print(f"=== ADD ({len(diff.to_add)}) ===", file=file)
    for m in diff.to_add:
        print(_format_member(m), file=file)

    print(f"=== UPDATE CREDENTIAL ({len(diff.to_update_credential)}) ===", file=file)
    for m, _u in diff.to_update_credential:
        print(_format_member(m), file=file)

    print(f"=== UPDATE POLICY ({len(diff.to_update_policy)}) ===", file=file)
    for m, _u in diff.to_update_policy:
        print(_format_member(m), file=file)

    print(f"=== DEACTIVATE ({len(diff.to_deactivate)}) ===", file=file)
    for u in diff.to_deactivate:
        print(_format_user(u), file=file)

    print(f"=== UNMAPPED ({len(diff.unmapped)}) ===", file=file)
    for m in diff.unmapped:
        print(_format_member(m), file=file)


def print_config_issues(issues: list[ConfigIssue], *, file: IO[str]) -> None:
    for issue in issues:
        print(f"{issue.path}: {issue.message}", file=file)


def _format_member(m: ResolvedMember) -> str:
    parts = [str(m.contact_id), m.display_name]
    if m.card_id is not None:
        parts.append(f"[card_last4={_last4(m.card_id)}]")
    if m.target_policy:
        parts.append(f"[policy={m.target_policy}]")
    return " ".join(parts)


def _format_user(u: UnifiUser) -> str:
    parts = [str(u.contact_id), u.display_name]
    if u.card_id is not None:
        parts.append(f"[card_last4={_last4(u.card_id)}]")
    if u.policy:
        parts.append(f"[policy={u.policy}]")
    return " ".join(parts)


def _last4(card_id: int) -> str:
    hex_str = format(card_id, "X")
    return hex_str[-4:] if len(hex_str) >= 4 else hex_str.rjust(4, "0")
```

(Note: `_last4` here uses raw `card_id` hex, not nfc_id. For show-diff this is acceptable because the operator is staring at the terminal during manual inspection and doesn't need cross-correlation with logs.)

- [ ] **Step 4: Run; verify all 3 tests pass**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Type-check and lint**

```bash
uv run mypy --strict src/door_sync/cli.py tests/test_cli.py
uv run ruff check src/door_sync/cli.py tests/test_cli.py
```

Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add src/door_sync/cli.py tests/test_cli.py
git commit -m "Add cli.py: pretty-printers for show-diff and validate-config"
```

---

## Task 8: Rewrite `__main__.py` with argparse + subcommands (with TDD)

**Files:**
- Rewrite: `src/door_sync/__main__.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Create `tests/test_main.py` with all 7 tests**

```python
"""Tests for door_sync.__main__ — CLI entry point.

Calls main(argv=[...]) directly with monkeypatched orchestrator.reconcile,
so the full subprocess is not needed.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from door_sync import __main__ as main_mod
from door_sync import orchestrator
from door_sync.config import (
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    OpsPaths,
    UnifiConfig,
)
from door_sync.models import (
    Diff,
    ReconcileResult,
    SafetyThresholds,
    TierMapping,
    TierRule,
)


def _build_config(tmp_path: Path) -> Config:
    return Config(
        cadence_seconds=600,
        civicrm=CivicrmConfig(
            host="https://civicrm.example.org", api_key="k",
            card_id_field="Door_Access.card_id",
        ),
        unifi=UnifiConfig(
            host="https://unifi.example.org:12445", api_key="k",
            tls_fingerprint="AB:" * 31 + "AB", facility_code=42,
        ),
        safety=SafetyThresholds(),
        tier_mapping=TierMapping(
            rules={"Gold": TierRule(resolution="tier", target_policy="p1", rank=100)}
        ),
        ops_paths=OpsPaths(
            audit_jsonl=tmp_path / "audit.jsonl",
            state_json=tmp_path / "state.json",
            alert_flag=tmp_path / "alert.flag",
        ),
    )


def _patch_config_load(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr(main_mod.config_mod, "load",
                        lambda **_: cfg)


def test_run_once_success_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    monkeypatch.setattr(
        orchestrator, "reconcile",
        lambda c, *, dry_run: ReconcileResult(halted=False, reason=None,
                                              diff=Diff([], [], [], [], [])),
    )

    rc = main_mod.main(argv=["run", "--once"])
    assert rc == 0


def test_run_once_halt_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    monkeypatch.setattr(
        orchestrator, "reconcile",
        lambda c, *, dry_run: ReconcileResult(
            halted=True, reason="mass_deactivate",
            diff=Diff([], [], [], [], []),
        ),
    )

    rc = main_mod.main(argv=["run", "--once"])
    assert rc == 1


def test_run_once_crash_writes_audit_alert_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)

    def _raise(c: Config, *, dry_run: bool) -> ReconcileResult:
        raise ConnectionError("boom")
    monkeypatch.setattr(orchestrator, "reconcile", _raise)

    with caplog.at_level(logging.ERROR):
        rc = main_mod.main(argv=["run", "--once"])

    assert rc == 2

    audit_line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit_line["event"] == "crashed"
    assert audit_line["exception"]["class"] == "ConnectionError"

    flag = tmp_path / "alert.flag"
    assert flag.exists()
    assert "boom" in flag.read_text()


def test_run_without_once_returns_64(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main_mod.main(argv=["run"])
    assert rc == 64
    captured = capsys.readouterr()
    assert "daemon mode not yet implemented" in captured.err


def test_validate_config_bad_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _bad(**_: Any) -> Config:
        raise ConfigError([ConfigIssue(path="unifi.host", message="must start with https://")])
    monkeypatch.setattr(main_mod.config_mod, "load", _bad)

    rc = main_mod.main(argv=["validate-config"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unifi.host" in captured.err
    assert "must start with https://" in captured.err


def test_validate_config_good_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config_load(monkeypatch, _build_config(tmp_path))

    rc = main_mod.main(argv=["validate-config"])
    assert rc == 0


def test_show_diff_prints_sections_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)

    # Patch the clients inside __main__'s show_diff path. Simplest: patch
    # orchestrator.CivicrmClient and orchestrator.UnifiClient — show_diff
    # imports them through orchestrator.
    class _Civi:
        def __init__(self, c: CivicrmConfig) -> None: pass
        def fetch_active(self) -> list[Any]: return []
        def __enter__(self) -> "_Civi": return self
        def __exit__(self, *_: Any) -> None: pass

    class _Unifi:
        def __init__(self, c: UnifiConfig, *, dry_run: bool = False) -> None: pass
        def fetch_users(self) -> list[Any]: return []
        def __enter__(self) -> "_Unifi": return self
        def __exit__(self, *_: Any) -> None: pass

    monkeypatch.setattr(main_mod, "CivicrmClient", _Civi)
    monkeypatch.setattr(main_mod, "UnifiClient", _Unifi)

    rc = main_mod.main(argv=["show-diff"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "=== ADD (0) ===" in captured.out
    assert "=== DEACTIVATE (0) ===" in captured.out

    # show-diff must NOT touch audit/state/alert
    assert not (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()
```

- [ ] **Step 2: Run; verify all fail (current __main__ has no `main(argv=)` signature)**

```bash
uv run pytest tests/test_main.py -v
```

Expected: errors about `main()` signature, missing `config_mod`, etc.

- [ ] **Step 3: Rewrite `src/door_sync/__main__.py`**

Replace the entire file content with:

```python
"""door-sync CLI entry point.

Subcommands:
  run --once [--dry-run]   Execute one reconcile cycle and exit.
  show-diff                Read-only: fetch + compute diff, pretty-print, exit.
  validate-config          Load config, print issues, exit 0 (ok) or 1 (bad).

Exit codes:
  0  success
  1  cycle halted by safety guards; config validation failed
  2  cycle crashed (exception escaped orchestrator)
 64  CLI usage error (argparse default; also bare `run` without --once)

Daemon mode (loop, SIGTERM handling) is not yet implemented — that arrives
with the scheduler slice.
"""

import argparse
import logging
import sys
from pathlib import Path

from door_sync import alert, audit, cli, config as config_mod, orchestrator, reconciler, tier_mapping
from door_sync.civicrm.client import CivicrmClient
from door_sync.config import Config, ConfigError
from door_sync.unifi.client import UnifiClient

_logger = logging.getLogger("door_sync")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(verbose=args.verbose)

    if args.subcommand == "run":
        return cmd_run(args)
    if args.subcommand == "show-diff":
        return cmd_show_diff(args)
    if args.subcommand == "validate-config":
        return cmd_validate_config(args)
    parser.print_help(sys.stderr)
    return 64


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="door-sync")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable DEBUG-level logging")
    p.add_argument("--config", type=Path, default=None,
                   help="Path to config.toml (default: $DOOR_SYNC_CONFIG_DIR/config.toml or ./config.toml)")
    p.add_argument("--env-file", dest="env_file", type=Path, default=None,
                   help="Path to env file (default: $DOOR_SYNC_CONFIG_DIR/env or ./.env)")

    sub = p.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser("run", help="Execute reconciliation cycles")
    run_p.add_argument("--once", action="store_true",
                       help="Run one cycle and exit (REQUIRED for now)")
    run_p.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Compute diff and log to audit but do not write to UniFi")

    sub.add_parser("show-diff", help="Read-only: print computed diff and exit")
    sub.add_parser("validate-config", help="Load config and print issues; exit 0 (ok) or 1 (bad)")

    return p


def _setup_logging(*, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_run(args: argparse.Namespace) -> int:
    if not args.once:
        print("daemon mode not yet implemented; pass --once", file=sys.stderr)
        return 64

    try:
        config = config_mod.load(config_path=args.config, env_path=args.env_file)
    except ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1

    try:
        result = orchestrator.reconcile(config, dry_run=args.dry_run)
    except Exception as exc:
        _logger.exception("orchestrator crashed")
        audit.log_crashed(exc, path=config.ops_paths.audit_jsonl)
        alert.raise_(f"crashed: {type(exc).__name__}: {exc}",
                     path=config.ops_paths.alert_flag)
        return 2

    return 1 if result.halted else 0


def cmd_show_diff(args: argparse.Namespace) -> int:
    try:
        config = config_mod.load(config_path=args.config, env_path=args.env_file)
    except ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1

    try:
        civicrm = CivicrmClient(config.civicrm)
        unifi = UnifiClient(config.unifi, dry_run=True)
        members = civicrm.fetch_active()
        resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in members]
        users = unifi.fetch_users()
        diff = reconciler.compute_diff(resolved, users)
    except Exception:
        _logger.exception("show-diff failed")
        return 2

    cli.print_diff(diff, file=sys.stdout)
    return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        config_mod.load(config_path=args.config, env_path=args.env_file)
    except ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run all 7 main tests**

```bash
uv run pytest tests/test_main.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Run the full suite to catch regressions**

```bash
uv run pytest
```

Expected: everything green.

- [ ] **Step 6: Type-check and lint**

```bash
uv run mypy --strict src tests
uv run ruff check .
```

Expected: both clean.

- [ ] **Step 7: Smoke test the CLI manually**

```bash
uv run door-sync --help
uv run door-sync run --help
uv run door-sync validate-config 2>&1 | head -20
```

Expected:
- `--help` shows the three subcommands
- `run --help` shows `--once` and `--dry-run`
- `validate-config` either succeeds (if `./config.toml` is valid) or prints issues; either way exits cleanly

- [ ] **Step 8: Commit**

```bash
git add src/door_sync/__main__.py tests/test_main.py
git commit -m "Rewrite __main__.py with run/show-diff/validate-config subcommands"
```

---

## Task 9: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the status line**

In `CLAUDE.md`, find:

```
**Status: in active development.** Pure modules (reconciler, safety, tier_mapping), the CiviCRM client, and the UniFi Access client are merged. Orchestrator, scheduler, audit, state, alert still TBD. Architecture is locked; see `docs/architecture.md` before adding code.
```

Replace with:

```
**Status: in active development.** Pure modules, CiviCRM client, UniFi Access client, orchestrator + ops stubs (audit JSONL, state JSON, alert flag-file) are merged. Scheduler (daemon loop, SIGTERM handling) and a real alert transport (SMTP/webhook) are the remaining slices. Architecture is locked; see `docs/architecture.md` before adding code.
```

- [ ] **Step 2: Update the Commands block**

In `CLAUDE.md`, find:

```bash
uv sync                       # install
uv run pytest                 # tests
uv run mypy --strict src tests   # type check (strict)
uv run ruff check .           # lint
uv run door-sync --once       # one reconcile cycle, exit
uv run door-sync --dry-run    # compute + log diff; no UniFi writes
```

Replace with:

```bash
uv sync                                       # install
uv run pytest                                 # tests
uv run mypy --strict src tests                # type check (strict)
uv run ruff check .                           # lint
uv run door-sync run --once                   # one reconcile cycle, exit
uv run door-sync run --once --dry-run         # compute + log diff; no UniFi writes
uv run door-sync show-diff                    # read-only: print computed diff
uv run door-sync validate-config              # load config, print issues, exit
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Update CLAUDE.md for orchestrator + subcommand CLI"
```

---

## Task 10: Update `docs/architecture.md`

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: Update §10 orchestrator code block to include audit/alert/state wiring**

Find in `docs/architecture.md` the code block starting `def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:` (around line 316). Replace the function body with:

```python
def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:
    civicrm = CivicrmClient(config.civicrm)
    unifi = UnifiClient(config.unifi, dry_run=dry_run)
    paths = config.ops_paths

    civi_members = civicrm.fetch_active()
    resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
    unifi_users = unifi.fetch_users()

    diff = reconciler.compute_diff(resolved, unifi_users)
    active_baseline = sum(1 for u in unifi_users if u.active)
    check = safety.check(diff, baseline=active_baseline, thresholds=config.safety)

    if check.halted:
        audit.log_halt(check.reason, diff, dry_run=dry_run,
                       path=paths.audit_jsonl,
                       facility_code=config.unifi.facility_code)
        alert.raise_(check.reason, path=paths.alert_flag)
        if not dry_run:
            state.write_halt(paths.state_json, check.reason)
        return ReconcileResult(halted=True, reason=check.reason, diff=diff)

    unifi.apply(diff)
    audit.log_applied(diff, dry_run=dry_run, path=paths.audit_jsonl,
                      facility_code=config.unifi.facility_code)
    if not dry_run:
        state.write_success(paths.state_json)
        alert.clear(path=paths.alert_flag)
    return ReconcileResult(halted=False, reason=None, diff=diff)
```

Leave the surrounding prose (§10 "Invariants" bullets) unchanged.

- [ ] **Step 2: Update §12 deferred-items table**

Find in `docs/architecture.md` the table under "## 12. What this document does not yet specify" (around line 385). Two rows change:

**Remove** the row:
```
| Audit log entry schema | `audit.py` + new §16 here | JSON line per record; fields TBD |
```

**Replace** the row:
```
| Alerting transport | `alert.py` + new §17 here | Likely SMTP or webhook to existing space ops channel |
```

With:
```
| Alerting transport | `alert.py` + new §17 here | Flag-file stub is shipped (presence = active alert, contents = reason); SMTP/webhook transport TBD |
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md
git commit -m "Update architecture §10 with audit/alert/state; close §12 audit-schema item"
```

---

## Final verification (after all tasks)

- [ ] **Run the full validation gauntlet**

```bash
uv run pytest
uv run mypy --strict src tests
uv run ruff check .
```

Expected: all green.

- [ ] **Manual smoke test** (only if a real `./config.toml` and `./.env` are present in dev — skip otherwise)

```bash
uv run door-sync validate-config
uv run door-sync show-diff | head -30
uv run door-sync run --once --dry-run
```

Expected: each completes without traceback; audit JSONL line appears after the `run --once --dry-run` (no state.json write, no alert flag).

---

## Summary of deliverables

| Module | LoC (est) | Tests |
|---|---|---|
| `models.py` (State addition) | +7 | (covered by state tests) |
| `state.py` | ~70 | 9 |
| `audit.py` | ~85 | 8 |
| `alert.py` | ~25 | 6 |
| `config.py` (OpsPaths) | +35 | 3 (in test_config) |
| `orchestrator.py` | ~45 | 7 |
| `cli.py` | ~50 | 3 |
| `__main__.py` (rewrite) | ~100 | 7 |
| docs updates | ~20 | — |
| **Total new test count: 43** | | |
