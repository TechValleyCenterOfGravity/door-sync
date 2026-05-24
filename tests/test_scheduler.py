"""Tests for door_sync.scheduler — daemon loop and signal handlers.

All tests inject `reconcile_fn` and `shutdown_event` so the loop never
touches real HTTP, never calls real time.sleep, and never installs
process-wide signal handlers (except the one test that explicitly
covers signal handling, which restores the previous handlers).
"""

import json
import os
import signal
import threading
from pathlib import Path

from door_sync import scheduler
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


def test_runs_once_when_event_preset(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    event = threading.Event()
    event.set()  # pre-set: loop should run one cycle then exit immediately

    calls: list[bool] = []

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
        calls.append(dry_run)
        return _ok_result()

    rc = scheduler.run_forever(
        cfg,
        shutdown_event=event,
        reconcile_fn=fake_reconcile,
    )

    assert rc == 0
    assert calls == [False]  # one cycle, dry_run defaulted to False


def test_loops_until_event_set(tmp_path: Path) -> None:
    cfg = _config(tmp_path, cadence_seconds=0)
    event = threading.Event()
    call_count = 0

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
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


def test_continues_on_cycle_exception(tmp_path: Path) -> None:
    cfg = _config(tmp_path, cadence_seconds=0)
    event = threading.Event()
    call_count = 0

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
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


def test_dry_run_propagates_to_reconcile_fn(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    event = threading.Event()
    event.set()
    recorded: list[bool] = []

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
        recorded.append(dry_run)
        return _ok_result()

    scheduler.run_forever(
        cfg,
        dry_run=True,
        shutdown_event=event,
        reconcile_fn=fake_reconcile,
    )

    assert recorded == [True]
