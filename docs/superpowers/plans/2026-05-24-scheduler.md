# Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the long-running daemon loop so `door-sync` can run unattended under systemd on the Pi, calling `orchestrator.reconcile()` on a polling cadence and exiting cleanly on SIGTERM/SIGINT.

**Architecture:** New `scheduler.py` module exposing `run_forever(config, *, dry_run, shutdown_event, reconcile_fn)`. Per-cycle crash handling factored into a shared `orchestrator.handle_crash()` helper used by both `--once` and daemon mode. CLI bare `door-sync run` becomes the daemon entry point. Systemd unit template shipped as a non-installed file.

**Tech Stack:** Python 3.12, stdlib `threading` and `signal`, `pytest`, `uv` for tooling. Sync only — no asyncio.

**Spec:** `docs/superpowers/specs/2026-05-24-scheduler-design.md`.

---

## File Structure

**Create:**
- `src/door_sync/scheduler.py` — daemon loop and signal handlers (one responsibility: drive `reconcile_fn` until shutdown).
- `tests/test_scheduler.py` — scheduler unit tests.
- `deploy/door-sync.service` — systemd unit template.

**Modify:**
- `src/door_sync/orchestrator.py` — add public `handle_crash()` helper.
- `tests/test_orchestrator.py` — test for `handle_crash()`.
- `src/door_sync/__main__.py` — collapse one-shot crash handling to use `handle_crash`; wire bare `run` to `scheduler.run_forever`; update docstring and `--once` help.
- `tests/test_main.py` — replace `test_run_without_once_returns_64` with daemon-mode tests; keep `--once` tests intact.
- `README.md` — add "deploy on the Pi" paragraph.

---

## Task 1: Add `orchestrator.handle_crash()` helper

Factor the "log + audit + alert" sequence that currently lives inline in `__main__.cmd_run` into a public helper on `orchestrator`. This is the shared seam used by both `--once` and the daemon loop.

**Files:**
- Modify: `src/door_sync/orchestrator.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Add `import logging` to the top of `tests/test_orchestrator.py` if it isn't already imported, then append to the bottom of the file:

```python
def test_handle_crash_logs_audits_and_raises_alert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config(tmp_path)
    exc = ConnectionError("boom")

    with caplog.at_level(logging.ERROR, logger="door_sync.orchestrator"):
        orchestrator.handle_crash(exc, paths=cfg.ops_paths)

    audit_line = json.loads(cfg.ops_paths.audit_jsonl.read_text().splitlines()[0])
    assert audit_line["event"] == "crashed"
    assert audit_line["exception"]["class"] == "ConnectionError"
    assert audit_line["exception"]["message"] == "boom"

    assert cfg.ops_paths.alert_flag.exists()
    flag_text = cfg.ops_paths.alert_flag.read_text()
    assert "crashed" in flag_text
    assert "ConnectionError" in flag_text
    assert "boom" in flag_text


def test_handle_crash_truncates_long_exception_messages(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    long_msg = "x" * 500
    orchestrator.handle_crash(RuntimeError(long_msg), paths=cfg.ops_paths)

    flag_text = cfg.ops_paths.alert_flag.read_text()
    # Truncation matches __main__'s current 200-char + "..." behavior.
    assert "..." in flag_text
    assert "x" * 200 in flag_text
    assert "x" * 201 not in flag_text
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_orchestrator.py::test_handle_crash_logs_audits_and_raises_alert -v
```

Expected: FAIL with `AttributeError: module 'door_sync.orchestrator' has no attribute 'handle_crash'`.

- [ ] **Step 3: Implement `handle_crash` in `orchestrator.py`**

Add to `src/door_sync/orchestrator.py`:

```python
import logging

from door_sync.config import OpsPaths

_logger = logging.getLogger("door_sync.orchestrator")


def handle_crash(exc: Exception, *, paths: OpsPaths) -> None:
    """Log + audit + alert on a reconcile cycle crash.

    Shared by one-shot (--once) and daemon mode so behavior stays symmetric.
    """
    _logger.exception("reconcile crashed: %s", exc)
    audit.log_crashed(exc, path=paths.audit_jsonl)
    exc_msg = str(exc)
    if len(exc_msg) > 200:
        exc_msg = exc_msg[:200] + "..."
    alert.raise_(
        f"crashed: {type(exc).__name__}: {exc_msg}",
        path=paths.alert_flag,
    )
```

Note: `_logger.exception()` requires an active exception context. When called from `except`, that's automatic. Here we are called from a test directly, not from an `except` block — but `_logger.exception` falls back to logging the message without traceback if no exception is active. The test above only asserts on the audit + alert side effects, not on the log record's `exc_info`, so this is fine. (Within `run_forever` and `cmd_run`, the call site is inside `except`, so traceback capture works as expected.)

- [ ] **Step 4: Run both new tests to verify they pass**

```bash
uv run pytest tests/test_orchestrator.py -k handle_crash -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run the full orchestrator test suite to confirm no regressions**

```bash
uv run pytest tests/test_orchestrator.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/door_sync/orchestrator.py tests/test_orchestrator.py
git commit -m "Add orchestrator.handle_crash() shared helper

Factor the log+audit+alert sequence currently inlined in __main__.cmd_run
into a public helper so the upcoming scheduler can call the same code
path on per-cycle crashes."
```

---

## Task 2: Collapse `__main__.cmd_run` to use `handle_crash`

Replace the inline crash handling in `cmd_run` with a single `orchestrator.handle_crash()` call. No behavior change — existing tests prove it.

**Files:**
- Modify: `src/door_sync/__main__.py:114-126`

- [ ] **Step 1: Edit `cmd_run` in `src/door_sync/__main__.py`**

Replace the existing `try/except` block in `cmd_run` (lines roughly 114-126) with:

```python
    try:
        result = orchestrator.reconcile(config, dry_run=args.dry_run)
    except Exception as exc:
        orchestrator.handle_crash(exc, paths=config.ops_paths)
        return 2

    return 1 if result.halted else 0
```

Also remove the now-unused imports if any are dangling. The `alert` and `audit` imports are still needed indirectly (orchestrator uses them), but the `__main__` module no longer references them directly. Check the import block at the top of `__main__.py` and remove `alert` and `audit` from the `from door_sync import` line if they are no longer used elsewhere in the file.

After edit, the import line that was previously:

```python
from door_sync import alert, audit, cli, orchestrator, reconciler, tier_mapping
```

becomes:

```python
from door_sync import cli, orchestrator, reconciler, tier_mapping
```

- [ ] **Step 2: Run the existing crash test to confirm same behavior**

```bash
uv run pytest tests/test_main.py::test_run_once_crash_writes_audit_alert_exits_two -v
```

Expected: PASS (audit JSONL written, alert flag exists, exit code 2 — identical to before).

- [ ] **Step 3: Run the full __main__ test suite**

```bash
uv run pytest tests/test_main.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Run lint + type check on the touched files**

```bash
uv run ruff check src/door_sync/__main__.py
uv run mypy --strict src/door_sync/__main__.py
```

Expected: no errors. (If mypy complains about unused imports having been removed, that's fine — it just means the cleanup worked.)

- [ ] **Step 5: Commit**

```bash
git add src/door_sync/__main__.py
git commit -m "Collapse cmd_run crash handling to use orchestrator.handle_crash

No behavior change; same audit + alert + exit-code-2 outcome. Drops the
now-unused alert and audit imports from __main__."
```

---

## Task 3: Create `scheduler.py` with signal handlers

Build the module skeleton and the `_install_signal_handlers` helper. Test signal handling first because it's the foundation everything else depends on.

**Files:**
- Create: `src/door_sync/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing signal-handler tests**

Create `tests/test_scheduler.py` with:

```python
"""Tests for door_sync.scheduler — daemon loop and signal handlers.

All tests inject `reconcile_fn` and `shutdown_event` so the loop never
touches real HTTP, never calls real time.sleep, and never installs
process-wide signal handlers (except the one test that explicitly
covers signal handling, which restores the previous handlers).
"""

import os
import signal
import threading
from pathlib import Path

import pytest

from door_sync import scheduler


def test_install_signal_handlers_sets_event_on_sigterm() -> None:
    event = threading.Event()
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        scheduler._install_signal_handlers(event)
        os.kill(os.getpid(), signal.SIGTERM)
        # Signal delivery is synchronous on the main thread; handler ran
        # before the next Python bytecode instruction.
        assert event.is_set()
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)


def test_install_signal_handlers_sets_event_on_sigint() -> None:
    event = threading.Event()
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        scheduler._install_signal_handlers(event)
        os.kill(os.getpid(), signal.SIGINT)
        assert event.is_set()
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_scheduler.py -v
```

Expected: FAIL with `ImportError: cannot import name 'scheduler' from 'door_sync'` (or `ModuleNotFoundError`).

- [ ] **Step 3: Create `src/door_sync/scheduler.py` with the minimum to pass**

```python
"""Long-running daemon loop for door-sync.

Drives orchestrator.reconcile() on a fixed cadence. Exits cleanly when
SIGTERM or SIGINT is received: the in-flight cycle finishes, then the
loop's Event.wait() returns and the function returns 0.

Per-cycle exceptions are caught and routed through orchestrator.handle_crash
so daemon behavior is symmetric with `door-sync run --once`. The daemon
itself does not exit on a single cycle failure; only signal-driven
shutdown ends the loop.
"""

import logging
import signal
import threading
from typing import Protocol

from door_sync import orchestrator
from door_sync.config import Config
from door_sync.models import ReconcileResult

_logger = logging.getLogger("door_sync.scheduler")


class ReconcileFn(Protocol):
    def __call__(self, config: Config, *, dry_run: bool) -> ReconcileResult: ...


def _install_signal_handlers(event: threading.Event) -> None:
    def _handler(signum: int, _frame: object) -> None:
        _logger.info(
            "shutdown signal received (%s); exiting after current cycle",
            signal.Signals(signum).name,
        )
        event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_scheduler.py -v
```

Expected: both signal-handler tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/door_sync/scheduler.py tests/test_scheduler.py
git commit -m "Add scheduler module skeleton with signal handlers

SIGTERM and SIGINT both set a shared shutdown Event. run_forever
loop body added in the next commit."
```

---

## Task 4: Add `run_forever` — immediate-on-startup behavior

First behavioral test: when called with a pre-set Event, `run_forever` runs exactly one reconcile cycle (immediate-on-startup) then exits cleanly.

**Files:**
- Modify: `src/door_sync/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Add a Config builder to the test file**

Add near the top of `tests/test_scheduler.py` (after imports):

```python
from door_sync.config import (
    CivicrmConfig,
    Config,
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


def _config(tmp_path: Path, *, cadence_seconds: int = 600) -> Config:
    return Config(
        cadence_seconds=cadence_seconds,
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


def _ok_result() -> ReconcileResult:
    return ReconcileResult(halted=False, reason=None, diff=Diff([], [], [], [], []))
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_scheduler.py`:

```python
def test_runs_once_when_event_preset(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    event = threading.Event()
    event.set()  # pre-set: loop should run one cycle then exit immediately

    calls: list[bool] = []

    def fake_reconcile(c: Config, *, dry_run: bool) -> ReconcileResult:
        calls.append(dry_run)
        return _ok_result()

    rc = scheduler.run_forever(
        cfg,
        shutdown_event=event,
        reconcile_fn=fake_reconcile,
    )

    assert rc == 0
    assert calls == [False]  # one cycle, dry_run defaulted to False
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_scheduler.py::test_runs_once_when_event_preset -v
```

Expected: FAIL with `AttributeError: module 'door_sync.scheduler' has no attribute 'run_forever'`.

- [ ] **Step 4: Add minimal `run_forever` to `scheduler.py`**

Append to `src/door_sync/scheduler.py`:

```python
def run_forever(
    config: Config,
    *,
    dry_run: bool = False,
    shutdown_event: threading.Event | None = None,
    reconcile_fn: ReconcileFn = orchestrator.reconcile,
) -> int:
    """Run reconcile_fn in a loop until shutdown_event is set. Returns 0."""
    if shutdown_event is None:
        shutdown_event = threading.Event()
        _install_signal_handlers(shutdown_event)

    while True:
        _logger.info("cycle start")
        try:
            reconcile_fn(config, dry_run=dry_run)
        except Exception as exc:
            orchestrator.handle_crash(exc, paths=config.ops_paths)
        _logger.info(
            "cycle complete; sleeping %ds", config.cadence_seconds
        )
        if shutdown_event.wait(timeout=config.cadence_seconds):
            break
    _logger.info("scheduler exited")
    return 0
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
uv run pytest tests/test_scheduler.py::test_runs_once_when_event_preset -v
```

Expected: PASS. The pre-set event makes `Event.wait()` return `True` immediately after the first cycle, breaking the loop.

- [ ] **Step 6: Commit**

```bash
git add src/door_sync/scheduler.py tests/test_scheduler.py
git commit -m "Add scheduler.run_forever with immediate-on-startup behavior

First reconcile fires before the first wait, so operators see activity
within seconds of systemctl start."
```

---

## Task 5: Test the loop iterates until the event is set

Verify multi-iteration behavior with a fake `reconcile_fn` that sets the event on its Nth call. Uses `cadence_seconds=0` so `Event.wait(timeout=0)` returns immediately without sleeping.

**Files:**
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py`:

```python
def test_loops_until_event_set(tmp_path: Path) -> None:
    cfg = _config(tmp_path, cadence_seconds=0)
    event = threading.Event()
    call_count = 0

    def fake_reconcile(c: Config, *, dry_run: bool) -> ReconcileResult:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            event.set()
        return _ok_result()

    rc = scheduler.run_forever(
        cfg,
        shutdown_event=event,
        reconcile_fn=fake_reconcile,
    )

    assert rc == 0
    assert call_count == 3
```

- [ ] **Step 2: Run the test to verify it passes**

The looping logic from Task 4 already handles this. Run:

```bash
uv run pytest tests/test_scheduler.py::test_loops_until_event_set -v
```

Expected: PASS. If it fails, the implementation in `run_forever` is wrong (likely missing the `while True:` or breaking too early).

- [ ] **Step 3: Commit**

```bash
git add tests/test_scheduler.py
git commit -m "Test scheduler loops until event is set"
```

---

## Task 6: Per-cycle crash recovery

Verify a fake `reconcile_fn` that raises on the first call but is followed by a successful call. The crash must invoke `orchestrator.handle_crash` (which writes audit + alert) and the loop must continue.

**Files:**
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Add `import json` to the top of `tests/test_scheduler.py`, then append to the bottom of the file:

```python
def test_continues_on_cycle_exception(tmp_path: Path) -> None:
    cfg = _config(tmp_path, cadence_seconds=0)
    event = threading.Event()
    call_count = 0

    def fake_reconcile(c: Config, *, dry_run: bool) -> ReconcileResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        event.set()
        return _ok_result()

    rc = scheduler.run_forever(
        cfg,
        shutdown_event=event,
        reconcile_fn=fake_reconcile,
    )

    assert rc == 0
    assert call_count == 2

    audit_line = json.loads(cfg.ops_paths.audit_jsonl.read_text().splitlines()[0])
    assert audit_line["event"] == "crashed"
    assert audit_line["exception"]["class"] == "RuntimeError"
    assert audit_line["exception"]["message"] == "boom"

    flag_text = cfg.ops_paths.alert_flag.read_text()
    assert "crashed" in flag_text
    assert "RuntimeError" in flag_text
    assert "boom" in flag_text
```

- [ ] **Step 2: Run the test to verify it passes**

The `try/except` from Task 4 already invokes `orchestrator.handle_crash`. Run:

```bash
uv run pytest tests/test_scheduler.py::test_continues_on_cycle_exception -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scheduler.py
git commit -m "Test scheduler continues past cycle exceptions

Each crash routes through orchestrator.handle_crash so the audit log
and alert flag both update; loop proceeds to the next cycle."
```

---

## Task 7: `dry_run` propagation

Verify the `dry_run` kwarg is passed through to `reconcile_fn` unchanged.

**Files:**
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py`:

```python
def test_dry_run_propagates_to_reconcile_fn(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    event = threading.Event()
    event.set()
    recorded: list[bool] = []

    def fake_reconcile(c: Config, *, dry_run: bool) -> ReconcileResult:
        recorded.append(dry_run)
        return _ok_result()

    scheduler.run_forever(
        cfg,
        dry_run=True,
        shutdown_event=event,
        reconcile_fn=fake_reconcile,
    )

    assert recorded == [True]
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/test_scheduler.py::test_dry_run_propagates_to_reconcile_fn -v
```

Expected: PASS (already implemented in Task 4 via `reconcile_fn(config, dry_run=dry_run)`).

- [ ] **Step 3: Run the entire scheduler test file to confirm everything still passes together**

```bash
uv run pytest tests/test_scheduler.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_scheduler.py
git commit -m "Test scheduler propagates dry_run to reconcile_fn"
```

---

## Task 8: Wire `cmd_run` to call `scheduler.run_forever`

Make bare `door-sync run` start the daemon. Replace the old `"daemon mode not yet implemented"` behavior with a real call to `scheduler.run_forever`. Replace the `--once` requirement test with daemon-mode tests that patch `scheduler.run_forever`.

**Files:**
- Modify: `src/door_sync/__main__.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write the failing tests in `tests/test_main.py`**

Replace the existing `test_run_without_once_returns_64` test with these two:

```python
def test_run_daemon_calls_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    recorded: dict[str, object] = {}

    def fake_run_forever(c: Config, *, dry_run: bool) -> int:
        recorded["config"] = c
        recorded["dry_run"] = dry_run
        return 0

    from door_sync import scheduler

    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)

    rc = main_mod.main(argv=["run"])

    assert rc == 0
    assert recorded["config"] is cfg
    assert recorded["dry_run"] is False


def test_run_daemon_with_dry_run_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    recorded: dict[str, object] = {}

    def fake_run_forever(c: Config, *, dry_run: bool) -> int:
        recorded["dry_run"] = dry_run
        return 0

    from door_sync import scheduler

    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)

    rc = main_mod.main(argv=["run", "--dry-run"])

    assert rc == 0
    assert recorded["dry_run"] is True
```

Delete the old `test_run_without_once_returns_64` (lines roughly 120-126 in the current `test_main.py`).

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
uv run pytest tests/test_main.py::test_run_daemon_calls_scheduler tests/test_main.py::test_run_daemon_with_dry_run_flag -v
```

Expected: FAIL — `cmd_run` currently prints "daemon mode not yet implemented" and returns 64.

- [ ] **Step 3: Update `src/door_sync/__main__.py`**

Add the scheduler import near the top:

```python
from door_sync import cli, orchestrator, reconciler, scheduler, tier_mapping
```

And update `__all__`:

```python
__all__ = ["config_mod", "CivicrmClient", "UnifiClient", "scheduler", "main"]
```

Replace the bare-`run` guard at the top of `cmd_run`. The function should now look like:

```python
def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = config_mod.load(config_path=args.config, env_path=args.env_file)
    except config_mod.ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1

    if not args.once:
        return scheduler.run_forever(config, dry_run=args.dry_run)

    try:
        result = orchestrator.reconcile(config, dry_run=args.dry_run)
    except Exception as exc:
        orchestrator.handle_crash(exc, paths=config.ops_paths)
        return 2

    return 1 if result.halted else 0
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
uv run pytest tests/test_main.py::test_run_daemon_calls_scheduler tests/test_main.py::test_run_daemon_with_dry_run_flag -v
```

Expected: both PASS.

- [ ] **Step 5: Run the full __main__ test suite to confirm no regressions**

```bash
uv run pytest tests/test_main.py -v
```

Expected: all tests PASS (the deleted `test_run_without_once_returns_64` is no longer collected; existing `--once`, `show-diff`, and `validate-config` tests still pass).

- [ ] **Step 6: Commit**

```bash
git add src/door_sync/__main__.py tests/test_main.py
git commit -m "Wire bare 'door-sync run' to scheduler.run_forever

Daemon mode is now the default for the run subcommand; --once keeps
its one-shot behavior. Removes the 'not yet implemented' guard."
```

---

## Task 9: Update `__main__.py` docstring and `--once` help text

Reflect the new daemon-default reality. Pure docs change.

**Files:**
- Modify: `src/door_sync/__main__.py:1-16, 73-78`

- [ ] **Step 1: Replace the module docstring**

In `src/door_sync/__main__.py`, replace the top docstring with:

```python
"""door-sync CLI entry point.

Subcommands:
  run [--dry-run]          Run the reconcile loop until SIGTERM/SIGINT
                           (daemon mode; default cadence 600s from config).
  run --once [--dry-run]   Execute one reconcile cycle and exit.
  show-diff                Read-only: fetch + compute diff, pretty-print, exit.
  validate-config          Load config, print issues, exit 0 (ok) or 1 (bad).

Exit codes:
  0  success (one-shot success; daemon clean shutdown)
  1  cycle halted by safety guards; config validation failed
  2  cycle crashed (--once only — daemon catches and continues)
 64  CLI usage error (argparse default)
"""
```

- [ ] **Step 2: Update the `--once` help text**

Replace:

```python
        help="Run one cycle and exit (REQUIRED for now)",
```

with:

```python
        help="Run one cycle and exit (default: run the daemon loop)",
```

- [ ] **Step 3: Run the test suite to confirm nothing broke**

```bash
uv run pytest tests/test_main.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/door_sync/__main__.py
git commit -m "Update __main__ docstring for daemon-default run subcommand"
```

---

## Task 10: Add `deploy/door-sync.service` systemd unit template

Ship the template the operator copies onto the Pi. No installer.

**Files:**
- Create: `deploy/door-sync.service`

- [ ] **Step 1: Create the directory and file**

```bash
mkdir -p deploy
```

Create `deploy/door-sync.service` with:

```ini
[Unit]
Description=door-sync: CiviCRM to UniFi Access reconciler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=door-sync
Group=door-sync
EnvironmentFile=/etc/door-sync/env
Environment=DOOR_SYNC_CONFIG_DIR=/etc/door-sync
ExecStart=/usr/local/bin/door-sync run
Restart=on-failure
RestartSec=30s
StandardOutput=journal
StandardError=journal

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/log/door-sync /var/lib/door-sync /var/run/door-sync

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Commit**

```bash
git add deploy/door-sync.service
git commit -m "Add systemd unit template for door-sync daemon

Template only — operators copy to /etc/systemd/system/ and adapt.
Hardened with NoNewPrivileges, ProtectSystem=strict, and an explicit
ReadWritePaths whitelist for the three ops directories."
```

---

## Task 11: Add deploy paragraph to README

Point operators at the new unit file.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read the current README to find a good insertion point**

Use the Read tool on `README.md`. Find the section that documents commands (the one that already lists `uv run door-sync run --once`). The deploy section goes after that section, or appended as a new top-level section if no obvious slot exists.

- [ ] **Step 2: Add the deploy section**

Add a new section to `README.md`:

```markdown
## Deploying on the Pi

`deploy/door-sync.service` is a systemd unit template. To install:

1. Copy the binary into place: `pip install ...` (or `uv tool install` from a checkout).
2. Create the service user: `sudo useradd --system --no-create-home door-sync`.
3. Create the config and ops directories:
   ```bash
   sudo mkdir -p /etc/door-sync /var/log/door-sync /var/lib/door-sync /var/run/door-sync
   sudo chown -R door-sync:door-sync /var/log/door-sync /var/lib/door-sync /var/run/door-sync
   ```
4. Drop `config.toml` into `/etc/door-sync/` (mode 0644) and `env` into the same dir (mode 0400).
5. Install the unit: `sudo cp deploy/door-sync.service /etc/systemd/system/`.
6. Start: `sudo systemctl daemon-reload && sudo systemctl enable --now door-sync`.

Stop with `sudo systemctl stop door-sync`; the daemon catches SIGTERM, finishes its in-flight reconcile, and exits 0.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document Pi deployment with the systemd unit template"
```

---

## Task 12: Full lint + type-check + test sweep

Confirm the whole slice is clean before declaring it done.

- [ ] **Step 1: Run lint**

```bash
uv run ruff check .
```

Expected: no errors.

- [ ] **Step 2: Run formatter check**

```bash
uv run ruff format --check .
```

Expected: no changes needed. If anything is misformatted, run `uv run ruff format .` and commit the result.

- [ ] **Step 3: Run strict type check**

```bash
uv run mypy --strict src tests
```

Expected: no errors. Common things to watch for:
- `scheduler.py`'s `_handler(signum: int, _frame: object)`: signal handler frame type is `types.FrameType | None`, not `object`. If mypy complains, change to `_frame: types.FrameType | None` and add `import types`. The standard library `signal.signal` accepts a callable matching `_HANDLER`, which uses `FrameType | None`.
- `ReconcileFn` Protocol may need `@runtime_checkable` if any code does `isinstance` checks; we don't, so leave it off.

- [ ] **Step 4: Run the full test suite**

```bash
uv run pytest
```

Expected: all tests PASS.

- [ ] **Step 5: Smoke-test the daemon manually (optional but recommended)**

If a `config.toml` and `.env` are available locally, spin the daemon up briefly to verify shutdown works:

```bash
uv run door-sync run --dry-run &
DAEMON_PID=$!
sleep 2
kill -TERM $DAEMON_PID
wait $DAEMON_PID
echo "Exit code: $?"
```

Expected: exit code 0, and the logs show `"cycle start"`, then `"shutdown signal received (SIGTERM); exiting after current cycle"`, then `"scheduler exited"`.

If no local config exists, skip this step — Task 6 already covers the crash-and-continue and clean-exit logic via tests.

- [ ] **Step 6: No additional commit needed unless format/lint touched files**

If everything was green, the slice is done. If `ruff format .` changed anything in Step 2, commit:

```bash
git add -u
git commit -m "Apply ruff format"
```

---

## Summary

Twelve tasks, each TDD where code changes are involved. The slice produces:
- A working daemon loop with signal handling.
- Symmetric crash handling between `--once` and daemon mode.
- A systemd unit template ready for the Pi.
- Updated docs.
- Full test coverage with no real signals beyond the dedicated handler-install test.

After Task 12, `door-sync run` is production-ready and the remaining roadmap item is the SMTP/webhook alert transport (separate slice).
