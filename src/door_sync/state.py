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
from datetime import UTC, datetime
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


def write_success(path: Path, *, now: datetime | None = None) -> None:
    current = read(path)
    when = now if now is not None else datetime.now(UTC)
    new = State(
        last_success_iso=_iso_z(when),
        last_halt_iso=current.last_halt_iso,
        last_halt_reason=current.last_halt_reason,
        run_count=current.run_count + 1,
    )
    _atomic_write(path, new)


def write_halt(path: Path, reason: str, *, now: datetime | None = None) -> None:
    current = read(path)
    when = now if now is not None else datetime.now(UTC)
    new = State(
        last_success_iso=current.last_success_iso,
        last_halt_iso=_iso_z(when),
        last_halt_reason=reason,
        run_count=current.run_count + 1,
    )
    _atomic_write(path, new)


def _iso_z(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
