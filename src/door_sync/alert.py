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
