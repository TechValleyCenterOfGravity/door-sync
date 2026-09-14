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
import queue
import signal
import threading
import time
import types
from typing import Protocol

from door_sync import orchestrator
from door_sync.config import Config
from door_sync.models import ReconcileResult

_logger = logging.getLogger("door_sync.scheduler")

# Enqueued by the signal handler so a blocked queue.get() wakes immediately.
_SHUTDOWN = object()

# queue.Queue cannot wait on a queue item and an Event at once, so the wait is
# sliced this finely to stay responsive to a caller-set shutdown_event.
_SHUTDOWN_POLL_SECONDS = 0.5


class ReconcileFn(Protocol):
    """Callable protocol for a single reconcile cycle."""

    def __call__(self, config: Config, *, dry_run: bool) -> ReconcileResult:
        """Run one reconcile cycle. Production impl: orchestrator.reconcile."""


def _install_signal_handlers(
    event: threading.Event,
    work_queue: "queue.Queue[object] | None" = None,
) -> None:
    def _handler(signum: int, _frame: types.FrameType | None) -> None:
        _logger.info(
            "shutdown signal received (%s); exiting after current cycle",
            signal.Signals(signum).name,
        )
        event.set()
        if work_queue is not None:
            try:
                work_queue.put_nowait(_SHUTDOWN)
            except queue.Full:  # pragma: no cover - queue is unbounded
                pass

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _get_until(
    work_queue: "queue.Queue[object]",
    shutdown_event: threading.Event,
    *,
    timeout: float,
) -> object:
    """``work_queue.get(timeout=...)``, but also wake promptly on `shutdown_event`.

    Returns the queue item, or `_SHUTDOWN` if the event is set while waiting.
    Raises `queue.Empty` if `timeout` elapses first. The signal handler enqueues
    the sentinel, but a caller that only sets the event must stop just as
    promptly -- `Event.wait` used to guarantee that.
    """
    deadline = time.monotonic() + timeout
    while True:
        if shutdown_event.is_set():
            return _SHUTDOWN
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            return work_queue.get(timeout=min(remaining, _SHUTDOWN_POLL_SECONDS))
        except queue.Empty:
            continue


def _wait_for_trigger(
    work_queue: "queue.Queue[object]",
    shutdown_event: threading.Event,
    *,
    cadence_seconds: float,
    debounce_seconds: float,
) -> bool:
    """Block until the next cycle should run.

    Returns True iff shutdown was requested (caller must exit); False on a
    normal wake (cadence tick or a coalesced work item). A cadence timeout, a
    work item, or the shutdown sentinel each end the wait. When woken by a work
    item, a short settle window drains any burst so many pending webhook
    triggers collapse into a single reconcile (debounce/coalesce).
    """
    if shutdown_event.is_set():
        return True
    try:
        item = _get_until(work_queue, shutdown_event, timeout=cadence_seconds)
    except queue.Empty:
        return shutdown_event.is_set()  # periodic cadence tick
    if item is _SHUTDOWN or shutdown_event.is_set():
        return True
    # Woken early by a reconcile trigger. Absorb a burst within the settle
    # window, discarding the extra triggers (all fold into the next cycle).
    deadline = time.monotonic() + debounce_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            nxt = _get_until(work_queue, shutdown_event, timeout=remaining)
        except queue.Empty:
            break
        if nxt is _SHUTDOWN or shutdown_event.is_set():
            return True
    return False


def run_forever(
    config: Config,
    *,
    dry_run: bool = False,
    shutdown_event: threading.Event | None = None,
    work_queue: "queue.Queue[object] | None" = None,
    reconcile_fn: ReconcileFn = orchestrator.reconcile,
) -> int:
    """Run reconcile cycles in a loop until a shutdown signal is received.

    The cadence is the idle upper bound between cycles; an enqueued
    ReconcileRequest wakes the loop early and is coalesced with any burst. The
    trigger always runs a FULL reconcile — orchestrator.reconcile's signature is
    unchanged.

    Args:
        config: Full application configuration (includes cadence_seconds and the
            webhook debounce window).
        dry_run: If True, all cycles run in dry-run mode.
        shutdown_event: Threading event to signal shutdown. When None,
            SIGTERM/SIGINT handlers are installed automatically.
        work_queue: Shared queue drained between cycles; the webhook receiver
            thread enqueues ReconcileRequests onto it. Created if None.
        reconcile_fn: Callable to execute each cycle. Defaults to
            `orchestrator.reconcile`.

    Returns:
        Always returns 0 (clean shutdown).
    """
    if work_queue is None:
        work_queue = queue.Queue()
    if shutdown_event is None:
        shutdown_event = threading.Event()
        _install_signal_handlers(shutdown_event, work_queue)

    while True:
        _logger.info("cycle start")
        try:
            reconcile_fn(config, dry_run=dry_run)
        except Exception as exc:
            orchestrator.handle_crash(exc, paths=config.ops_paths, alert_config=config.alert)
        _logger.info("cycle complete; waiting up to %ds", config.cadence_seconds)
        if _wait_for_trigger(
            work_queue,
            shutdown_event,
            cadence_seconds=config.cadence_seconds,
            debounce_seconds=config.webhook.debounce_seconds,
        ):
            break
    _logger.info("scheduler exited")
    return 0
