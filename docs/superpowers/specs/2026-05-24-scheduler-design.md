# Scheduler — Design

**Date:** 2026-05-24
**Status:** Approved, pending implementation
**Companion:** `docs/architecture.md` §3 (process model), §4 (layering)

## 1. Purpose

Add the long-running daemon loop that `door-sync` runs under systemd on the Pi. The loop calls `orchestrator.reconcile()` on a polling cadence and exits cleanly on SIGTERM/SIGINT. This is the last code slice required before the service can run unattended.

## 2. Scope

In scope:
- New module `src/door_sync/scheduler.py` exposing `run_forever()`.
- Per-cycle exception handling that mirrors one-shot `--once` behavior (audit log + alert flag), factored into a shared helper.
- CLI: bare `door-sync run` becomes the daemon entry point; `run --once` keeps its existing one-shot behavior. `--dry-run` works in both modes.
- A systemd unit template at `deploy/door-sync.service` (file only; no installer).
- Tests covering loop behavior, crash recovery, signal handling, and CLI wiring.

Out of scope:
- `--cadence-override` CLI flag (cadence stays in TOML).
- Catch-up logic when a cycle overruns the cadence (the orchestrator is idempotent; a single sleep is fine).
- Per-cycle metrics export (state JSON is sufficient for now).
- `logrotate.d` configuration for `audit.jsonl`.
- PID file handling (systemd tracks the PID).
- Service-user creation, installer scripts, or `/etc/door-sync` provisioning.

## 3. Module: `scheduler.py`

### Public API

```python
def run_forever(
    config: Config,
    *,
    dry_run: bool = False,
    shutdown_event: threading.Event | None = None,
    reconcile_fn: ReconcileFn = orchestrator.reconcile,
) -> int:
    """Run reconcile in a loop until shutdown_event is set. Returns 0 on clean exit."""
```

`ReconcileFn` is a Protocol (or `Callable` type alias) matching `orchestrator.reconcile`'s actual signature: `(config: Config, *, dry_run: bool) -> ReconcileResult`. A bare `Callable[[Config, bool], ReconcileResult]` would lose the keyword-only constraint and lie about the call shape.

Dependency-injection points:
- `shutdown_event`: tests pass a pre-set or test-controlled Event. When `None`, the function creates one and installs signal handlers.
- `reconcile_fn`: tests pass a fake that records calls, raises, or sets the event.

### Private helpers

- `_install_signal_handlers(event: threading.Event) -> None` — registers SIGTERM and SIGINT handlers that call `event.set()` and log `"shutdown signal received; exiting after current cycle"`. Only called when `run_forever` creates its own Event, so tests passing their own Event do not perturb pytest's signal handling.

### Per-cycle sequence

```
loop:
    log INFO "cycle start"
    try:
        reconcile_fn(config, dry_run=dry_run)
    except Exception as exc:
        orchestrator.handle_crash(exc, paths=config.ops_paths)
    log INFO "cycle complete; sleeping {cadence}s"
    if shutdown_event.wait(timeout=config.cadence_seconds):
        break
log INFO "scheduler exited"
return 0
```

Notes:
- `reconcile_fn` return value is discarded. The orchestrator already records halts in audit + alert; nothing in the loop needs the `ReconcileResult`.
- First reconcile fires immediately on startup (Q1 decision). The wait happens *after* the cycle, not before.
- `Event.wait(timeout=...)` returns `True` if the event was set, `False` on timeout. Set → break; timeout → next iteration.
- Crashes inside the loop never escape `run_forever`. The daemon does not exit on a single cycle failure; systemd `Restart=on-failure` only triggers if something *outside* the loop crashes (signal handler install failure, etc.).

## 4. Shared crash-handling helper

Both one-shot (`__main__.py:cmd_run`) and daemon mode need identical behavior when `reconcile()` raises:

1. `_logger.exception("…")` (logger name differs per caller; that's fine).
2. `audit.log_crashed(exc, path=paths.audit_jsonl)`.
3. `alert.raise_("crashed: …", path=paths.alert_flag)` with the same truncated-message format `__main__.py` currently uses.

Factor into `orchestrator.handle_crash(exc: Exception, *, paths: OpsPaths) -> None`. `Exception`, not `BaseException` — architecture §3 specifies `try/except Exception`, and we install our own SIGINT handler so `KeyboardInterrupt` should never reach this code path inside the daemon. Lives in `orchestrator.py` because it already owns `paths` and already imports `audit` + `alert`. Both `__main__.cmd_run` (one-shot branch) and `scheduler.run_forever` call it. No behavior change for `--once`; the existing inline implementation collapses to one line.

## 5. CLI changes (`__main__.py`)

- Remove the `if not args.once: return 64` guard in `cmd_run`.
- `cmd_run`: if `args.once`, run the existing one-shot flow; else call `scheduler.run_forever(config, dry_run=args.dry_run)` and return its exit code.
- Drop `--once`'s `"REQUIRED for now"` help text.
- Update the module docstring: remove the "Daemon mode … not yet implemented" paragraph; document that bare `run` is the daemon and `run --once` is one-shot. Remove the `64` exit code's mention of bare-`run`; keep `64` for argparse usage errors only.

## 6. systemd unit (`deploy/door-sync.service`)

Template file. Operators copy and adapt; the repo does not install it.

```ini
[Unit]
Description=door-sync: CiviCRM ↔ UniFi Access reconciler
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

README gets a one-paragraph "deploy on the Pi" note pointing at this file.

## 7. Tests (`tests/test_scheduler.py`)

All tests inject `reconcile_fn` and `shutdown_event`. No real HTTP, no real `time.sleep`, no real signals (except the one signal-handler test).

1. **`test_runs_once_when_event_preset`** — `shutdown_event` is pre-set before calling `run_forever`; assert `reconcile_fn` is called exactly once (immediate-on-startup behavior) and the function returns 0.
2. **`test_loops_until_event_set`** — fake `reconcile_fn` that sets the event on its 3rd call; cadence configured to a small value (e.g., 0.01s); assert call count == 3.
3. **`test_continues_on_cycle_exception`** — fake `reconcile_fn` raises `RuntimeError("boom")` on call 1, succeeds on call 2 (which sets the event). Assert: both calls happened; `audit.log_crashed` was invoked with the exception; `alert.raise_` was invoked with a message containing `"crashed"` and `"boom"`.
4. **`test_signal_sets_event`** — call `_install_signal_handlers(event)`; `os.kill(os.getpid(), signal.SIGTERM)`; assert `event.is_set()`. Same for SIGINT. Restore the original handlers in a `finally` so subsequent tests aren't affected.
5. **`test_dry_run_propagates`** — fake `reconcile_fn` records its `dry_run` kwarg; `run_forever(..., dry_run=True, shutdown_event=preset)` → recorded kwarg is `True`.
6. **CLI tests (in `test_main.py`)**:
   - `cmd_run` without `--once` patches `scheduler.run_forever` and asserts it was called with `dry_run=False`.
   - `cmd_run` with `--dry-run` (no `--once`) asserts `dry_run=True`.
   - Do NOT exercise the real loop from `test_main.py`.

## 8. Layering (architecture.md §4)

- `scheduler` imports: `orchestrator`, `config`, `models`, plus stdlib (`threading`, `signal`, `logging`). Matches the table.
- `orchestrator` gains `handle_crash`; its imports do not change.
- `__main__` imports `scheduler`. Already imports `orchestrator`; one more is fine.
- Nothing else imports `scheduler`. The future `webhook` slice will share the same `shutdown_event` but is not part of this work.

## 9. Risks / non-obvious behavior

- **Signal handlers and pytest:** pytest installs its own SIGINT handler. `_install_signal_handlers` only runs when `run_forever` creates its own Event — tests pass their own, so handler install is skipped. Test #4 saves and restores the original handlers explicitly.
- **Crash alert flag persistence:** the alert flag stays raised across cycles until a successful cycle clears it (orchestrator already calls `alert.clear` on success). This is intentional — operators see "something was broken recently" even after recovery.
- **Dry-run safety halts still alert:** orchestrator already raises the alert flag when safety halts even in dry-run (existing intentional behavior, see comment in `orchestrator.py`). The daemon does not need to override this.
- **Cycle overrun:** if a cycle takes longer than `cadence_seconds`, the next sleep is still the full cadence. No catch-up. Acceptable because reconcile is idempotent and the budget is "minutes, not milliseconds" (architecture.md §2).
