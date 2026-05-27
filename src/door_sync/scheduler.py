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
import types
from typing import Protocol

from door_sync import orchestrator
from door_sync.config import Config
from door_sync.models import ReconcileResult

_logger = logging.getLogger("door_sync.scheduler")


class ReconcileFn(Protocol):
    """Callable protocol for a single reconcile cycle."""

    def __call__(self, config: Config, *, dry_run: bool) -> ReconcileResult:
        """Run one reconcile cycle. Production impl: orchestrator.reconcile."""


def _install_signal_handlers(event: threading.Event) -> None:
    def _handler(signum: int, _frame: types.FrameType | None) -> None:
        _logger.info(
            "shutdown signal received (%s); exiting after current cycle",
            signal.Signals(signum).name,
        )
        event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run_forever(
    config: Config,
    *,
    dry_run: bool = False,
    shutdown_event: threading.Event | None = None,
    reconcile_fn: ReconcileFn = orchestrator.reconcile,
) -> int:
    """Run reconcile cycles in a loop until a shutdown signal is received.

    Args:
        config: Full application configuration (includes cadence_seconds).
        dry_run: If True, all cycles run in dry-run mode.
        shutdown_event: Threading event to signal shutdown. When None,
            SIGTERM/SIGINT handlers are installed automatically.
        reconcile_fn: Callable to execute each cycle. Defaults to
            `orchestrator.reconcile`.

    Returns:
        Always returns 0 (clean shutdown).
    """
    if shutdown_event is None:
        shutdown_event = threading.Event()
        _install_signal_handlers(shutdown_event)

    while True:
        _logger.info("cycle start")
        try:
            reconcile_fn(config, dry_run=dry_run)
        except Exception as exc:
            orchestrator.handle_crash(exc, paths=config.ops_paths, alert_config=config.alert)
        _logger.info("cycle complete; sleeping %ds", config.cadence_seconds)
        if shutdown_event.wait(timeout=config.cadence_seconds):
            break
    _logger.info("scheduler exited")
    return 0
