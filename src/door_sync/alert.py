"""Alert dispatch for door-sync.

Flag-file alerting (presence = active alert) plus optional email
transports (SMTP or Mailgun HTTP API). The flag file is always written
regardless of transport — external monitoring (Nagios, Prometheus
textfile collector, etc.) can detect halts without parsing logs.

Every failing cycle sends an ALERT, including consecutive failures on the same
condition. RESOLVED is edge-triggered on the flag: it goes out once, on the
cycle that clears an alert that was actually active, so a healthy daemon does
not mail after every sync.

Email failures are logged at ERROR but never crash a reconcile cycle.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from door_sync.config import AlertConfig, MailgunConfig, SmtpConfig

_logger = logging.getLogger("door_sync.alert")

_MAILGUN_API_BASE = "https://api.mailgun.net/v3"


def raise_(
    reason: str,
    *,
    path: Path,
    alert_config: AlertConfig | None = None,
) -> None:
    """Write flag file and, if configured, send an alert email.

    Every failing cycle mails, including consecutive failures on the same
    condition. A halt that repeats is a sync that is still not happening, and
    each one is worth surfacing -- only RESOLVED is edge-triggered, so that a
    healthy daemon stays silent (see `clear()`).

    Args:
        reason: Human-readable description of the alert condition.
        path: Path to the alert flag file.
        alert_config: Email transport settings, or None for flag-file only.
    """
    _logger.error("ALERT: %s", reason)
    _write_flag(reason, path)
    if alert_config is not None:
        _dispatch(alert_config, subject="ALERT", body=reason)


def clear(
    *,
    path: Path,
    alert_config: AlertConfig | None = None,
) -> None:
    """Remove flag file and, if an alert was active, send a resolved email.

    RESOLVED is edge-triggered on the flag file: it goes out only on the cycle
    that actually removes a flag, so a healthy daemon does not mail the
    operator once per reconcile. A cycle that finds no flag has nothing to
    resolve and says nothing.

    If the flag could not be written when the alert was raised, no RESOLVED
    follows that alert -- the unwritable path is already on the logger.

    Args:
        path: Path to the alert flag file.
        alert_config: Email transport settings, or None for flag-file only.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        # No alert was active. Nothing to clear, nobody to notify.
        return
    except OSError as exc:
        # A flag that cannot be cleared errs toward alarming, which is the safe
        # direction, but the operator needs to know why it is stuck. No RESOLVED
        # either: the flag is still there, so this was not a transition, and a
        # stuck flag would otherwise mail on every subsequent cycle.
        _logger.error("could not clear alert flag %s: %s", path, exc)
        return
    if alert_config is not None:
        _dispatch(alert_config, subject="RESOLVED", body="Previous alert cleared.")


def _write_flag(reason: str, path: Path) -> None:
    """Write the flag atomically; never let an unwritable path escalate.

    The flag is a monitoring signal, not the halt itself -- the reason is
    already on the logger by the time this runs. If the path cannot be written
    (a stale absolute path in a device-provisioned config, a full disk, a
    directory the service user cannot reach) that must not turn a controlled
    halt into an unhandled exception, nor raise from inside the crash handler
    that calls this.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(reason + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        _logger.error("could not write alert flag %s: %s", path, exc)


def _dispatch(config: AlertConfig, *, subject: str, body: str) -> None:
    if config.transport == "smtp" and config.smtp is not None:
        _send_smtp(config.smtp, subject=subject, body=body)
    elif config.transport == "mailgun" and config.mailgun is not None:
        _send_mailgun(config.mailgun, subject=subject, body=body)


def _send_smtp(cfg: SmtpConfig, *, subject: str, body: str) -> None:
    full_subject = f"{cfg.subject_prefix} {subject}"
    msg = EmailMessage()
    msg["Subject"] = full_subject
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to_addrs)
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        if cfg.starttls:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
            server.starttls(context=ctx)
        else:
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=ctx)
        with server:
            server.login(cfg.username, cfg.password)
            server.send_message(msg)
        _logger.info("alert email sent via SMTP: %s", subject)
    except Exception as exc:
        _logger.error("failed to send alert email via SMTP", exc_info=exc)


def _send_mailgun(cfg: MailgunConfig, *, subject: str, body: str) -> None:
    full_subject = f"{cfg.subject_prefix} {subject}"
    url = f"{_MAILGUN_API_BASE}/{cfg.domain}/messages"
    try:
        resp = httpx.post(
            url,
            auth=("api", cfg.api_key),
            data={
                "from": cfg.from_addr,
                "to": list(cfg.to_addrs),
                "subject": full_subject,
                "text": body,
            },
            timeout=30,
        )
        resp.raise_for_status()
        _logger.info("alert email sent via Mailgun: %s", subject)
    except Exception as exc:
        _logger.error("failed to send alert email via Mailgun", exc_info=exc)
