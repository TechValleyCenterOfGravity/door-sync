"""Append-only JSONL audit log for door-sync.

One JSON object per line, one line per cycle outcome. Compatible with
logrotate copytruncate (open-append per write; no long-lived handle).
Card IDs are recorded as the last 4 chars of nfc_id hex, never as the
raw integer or full hex, per architecture §11.
"""

import json
from datetime import UTC, datetime
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


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(diff: Diff) -> dict[str, int]:
    return {
        "added": len(diff.to_add),
        "updated_credential": len(diff.to_update_credential),
        "updated_policy": len(diff.to_update_policy),
        "deactivated": len(diff.to_deactivate),
        "unmapped": len(diff.unmapped),
    }


def _card_last4(diff: Diff, facility_code: int) -> dict[str, list[str]]:
    # to_update_policy carries cards but the change is policy-only;
    # unmapped entries are intentionally not actioned. Both omitted to keep
    # card_last4 focused on cards that were actually written or revoked.
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
    return format(nfc_id, "X")[-4:]


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
