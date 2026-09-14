"""Tests for door_sync.scheduler — daemon loop and signal handlers.

All tests inject `reconcile_fn` and `shutdown_event` so the loop never
touches real HTTP, never calls real time.sleep, and never installs
process-wide signal handlers (except the one test that explicitly
covers signal handling, which restores the previous handlers).
"""

import json
import logging
import os
import queue
import signal
import threading
import time
from pathlib import Path

from door_sync import scheduler
from door_sync.config import (
    AlertConfig,
    CivicrmConfig,
    Config,
    OpsPaths,
    UnifiConfig,
    WebhookConfig,
)
from door_sync.models import (
    Diff,
    ReconcileRequest,
    ReconcileResult,
    SafetyThresholds,
    TierMapping,
    TierRule,
)


def _config(tmp_path: Path, *, cadence_seconds: int = 600, debounce_seconds: float = 0.0) -> Config:
    return Config(
        cadence_seconds=cadence_seconds,
        civicrm=CivicrmConfig(
            host="https://civicrm.example.org",
            api_key="k",
            card_id_field="Door_Access.card_id",
            active_statuses=("Current", "Grace"),
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
        alert=AlertConfig(transport="flag-file", smtp=None, mailgun=None),
        webhook=WebhookConfig(
            enabled=False,
            host="127.0.0.1",
            port=8787,
            hmac_secret="",
            max_body_bytes=65536,
            max_skew_seconds=300,
            debounce_seconds=debounce_seconds,
        ),
    )


def _ok_result() -> ReconcileResult:
    return ReconcileResult(halted=False, reason=None, diff=Diff((), (), (), (), ()))


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


# --- webhook wake / coalesce additions ---


def test_signal_handler_does_not_touch_the_work_queue() -> None:
    """The handler must stay async-signal-safe: it sets the Event and nothing
    else. Enqueuing here would take queue.Queue's non-reentrant mutex, which the
    main thread may already hold inside get() -- a self-deadlock that hangs
    shutdown until SIGKILL. _get_until polls the Event instead."""
    event = threading.Event()
    q: queue.Queue[object] = queue.Queue()
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        scheduler._install_signal_handlers(event)
        os.kill(os.getpid(), signal.SIGTERM)
        assert event.is_set()
        assert q.empty()
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)


def test_wait_for_trigger_returns_true_when_event_preset() -> None:
    q: queue.Queue[object] = queue.Queue()
    event = threading.Event()
    event.set()
    result = scheduler._wait_for_trigger(q, event, cadence_seconds=600, debounce_seconds=0.0)
    assert result.halted is True


def test_wait_for_trigger_consumes_work_item_without_full_cadence() -> None:
    q: queue.Queue[object] = queue.Queue()
    q.put(ReconcileRequest(reason="membership-changed"))
    event = threading.Event()
    result = scheduler._wait_for_trigger(q, event, cadence_seconds=600, debounce_seconds=0.0)
    assert result.halted is False
    assert q.empty()


def test_wait_for_trigger_shutdown_sentinel_returns_true() -> None:
    q: queue.Queue[object] = queue.Queue()
    q.put(scheduler._SHUTDOWN)
    event = threading.Event()
    result = scheduler._wait_for_trigger(q, event, cadence_seconds=600, debounce_seconds=0.0)
    assert result.halted is True


def test_wait_for_trigger_coalesces_burst_into_one_wait() -> None:
    q: queue.Queue[object] = queue.Queue()
    for _ in range(3):
        q.put(ReconcileRequest(reason="membership-changed"))
    event = threading.Event()
    result = scheduler._wait_for_trigger(q, event, cadence_seconds=600, debounce_seconds=0.05)
    assert result.halted is False
    assert q.empty()  # all three drained within the settle window


def test_wait_for_trigger_returns_false_on_cadence_tick() -> None:
    q: queue.Queue[object] = queue.Queue()
    event = threading.Event()
    result = scheduler._wait_for_trigger(q, event, cadence_seconds=0.01, debounce_seconds=0.0)
    assert result.halted is False


def test_run_forever_wakes_on_enqueued_item(tmp_path: Path) -> None:
    # Large cadence: only the queued item can drive the second cycle.
    cfg = _config(tmp_path, cadence_seconds=600)
    q: queue.Queue[object] = queue.Queue()
    q.put(ReconcileRequest(reason="membership-changed"))
    event = threading.Event()
    call_count = 0

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            event.set()
        return _ok_result()

    rc = scheduler.run_forever(cfg, shutdown_event=event, work_queue=q, reconcile_fn=fake_reconcile)
    assert rc == 0
    assert call_count == 2


def test_run_forever_breaks_on_shutdown_sentinel(tmp_path: Path) -> None:
    cfg = _config(tmp_path, cadence_seconds=600)
    q: queue.Queue[object] = queue.Queue()
    event = threading.Event()
    call_count = 0

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        # Simulate the signal handler firing during the cycle.
        event.set()
        q.put(scheduler._SHUTDOWN)
        return _ok_result()

    rc = scheduler.run_forever(cfg, shutdown_event=event, work_queue=q, reconcile_fn=fake_reconcile)
    assert rc == 0
    assert call_count == 1


def test_wait_for_trigger_honours_caller_set_shutdown_event() -> None:
    """Regression: the queue-driven wait replaced Event.wait(timeout=...), which
    returned as soon as the event was set. A caller that passes its own
    shutdown_event (the documented public path) and never enqueues the sentinel
    would otherwise block for the full cadence -- 600s by default."""
    work_queue: queue.Queue[object] = queue.Queue()
    event = threading.Event()
    timer = threading.Timer(0.1, event.set)
    timer.start()
    try:
        start = time.monotonic()
        wake = scheduler._wait_for_trigger(
            work_queue, event, cadence_seconds=30.0, debounce_seconds=0.0
        )
        elapsed = time.monotonic() - start
    finally:
        timer.cancel()
    assert wake.halted is True
    assert elapsed < 2.0, f"blocked {elapsed:.1f}s; should wake on the event"


def test_wait_for_trigger_honours_shutdown_event_during_debounce() -> None:
    """Same guarantee inside the burst-coalescing settle window."""
    work_queue: queue.Queue[object] = queue.Queue()
    work_queue.put(ReconcileRequest(reason="membership-changed", contact_id=1))
    event = threading.Event()
    timer = threading.Timer(0.1, event.set)
    timer.start()
    try:
        start = time.monotonic()
        wake = scheduler._wait_for_trigger(
            work_queue, event, cadence_seconds=30.0, debounce_seconds=30.0
        )
        elapsed = time.monotonic() - start
    finally:
        timer.cancel()
    assert wake.halted is True
    assert elapsed < 2.0, f"blocked {elapsed:.1f}s; should wake on the event"


def test_cycle_start_log_names_the_trigger(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """An operator reading the ops log could not tell a webhook-driven cycle
    from a cadence tick; the ReconcileRequest fields were written and never
    read. contact_id only -- never names (architecture.md §11)."""
    cfg = _config(tmp_path, cadence_seconds=600)
    q: queue.Queue[object] = queue.Queue()
    q.put(ReconcileRequest(reason="membership-changed", contact_id=4242))
    event = threading.Event()
    calls = 0

    def fake_reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls >= 2:
            event.set()
        return _ok_result()

    with caplog.at_level(logging.INFO, logger="door_sync.scheduler"):
        scheduler.run_forever(cfg, shutdown_event=event, work_queue=q, reconcile_fn=fake_reconcile)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cycle start (startup)" in text
    assert "trigger=membership-changed" in text
    assert "contact_id=4242" in text


def test_wake_reports_coalesced_count() -> None:
    q: queue.Queue[object] = queue.Queue()
    q.put(ReconcileRequest(reason="membership-changed", contact_id=1))
    for _ in range(3):
        q.put(ReconcileRequest(reason="membership-changed", contact_id=2))
    wake = scheduler._wait_for_trigger(
        q, threading.Event(), cadence_seconds=600, debounce_seconds=0.05
    )
    assert wake.halted is False
    assert wake.trigger is not None
    assert wake.trigger.contact_id == 1  # the first of the burst
    assert wake.coalesced == 3
