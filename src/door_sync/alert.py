"""Alert dispatch for door-sync.

Flag-file alerting (presence = active alert) plus optional email
transports (SMTP or Mailgun HTTP API). The flag file is always written
regardless of transport — external monitoring (Nagios, Prometheus
textfile collector, etc.) can detect halts without parsing logs.

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
    """Remove flag file and, if configured, send a resolved email.

    Args:
        path: Path to the alert flag file.
        alert_config: Email transport settings, or None for flag-file only.
    """
    path.unlink(missing_ok=True)
    if alert_config is not None:
        _dispatch(alert_config, subject="RESOLVED", body="Previous alert cleared.")


def _write_flag(reason: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(reason + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


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
