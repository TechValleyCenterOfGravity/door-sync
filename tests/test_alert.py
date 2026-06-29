"""Tests for door_sync.alert — flag-file and email transports."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from door_sync import alert
from door_sync.config import AlertConfig, MailgunConfig, SmtpConfig


def test_raise_creates_file_with_reason(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("mass_deactivate exceeded threshold", path=path)

    assert path.exists()
    assert path.read_text(encoding="utf-8") == "mass_deactivate exceeded threshold\n"


def test_raise_overwrites_previous_reason(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("first reason", path=path)
    alert.raise_("second reason", path=path)

    assert path.read_text(encoding="utf-8") == "second reason\n"


def test_clear_removes_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("a reason", path=path)
    assert path.exists()

    alert.clear(path=path)
    assert not path.exists()


def test_clear_missing_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "never_existed.flag"

    alert.clear(path=path)  # should not raise
    assert not path.exists()


def test_raise_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "alert.flag"
    alert.raise_("reason", path=path)

    assert path.exists()


def test_raise_logs_error_with_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "alert.flag"

    with caplog.at_level(logging.ERROR, logger="door_sync.alert"):
        alert.raise_("the reason", path=path)

    assert any(
        record.levelno == logging.ERROR and "the reason" in record.getMessage()
        for record in caplog.records
    )


# --- flag-file-only config passes through without sending email ---


def test_raise_flag_file_transport_does_not_send_email(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    cfg = AlertConfig(transport="flag-file", smtp=None, mailgun=None)

    with (
        patch.object(alert, "_send_smtp") as mock_smtp,
        patch.object(alert, "_send_mailgun") as mock_mg,
    ):
        alert.raise_("reason", path=path, alert_config=cfg)

    mock_smtp.assert_not_called()
    mock_mg.assert_not_called()
    assert path.exists()


def test_clear_flag_file_transport_does_not_send_email(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    path.write_text("reason\n")
    cfg = AlertConfig(transport="flag-file", smtp=None, mailgun=None)

    with (
        patch.object(alert, "_send_smtp") as mock_smtp,
        patch.object(alert, "_send_mailgun") as mock_mg,
    ):
        alert.clear(path=path, alert_config=cfg)

    mock_smtp.assert_not_called()
    mock_mg.assert_not_called()
    assert not path.exists()


# --- Mailgun transport ---


def _mailgun_config() -> MailgunConfig:
    return MailgunConfig(
        domain="mg.example.com",
        api_key="key-test",
        from_addr="door-sync@mg.example.com",
        to_addrs=("admin@example.com",),
        subject_prefix="[door-sync]",
    )


def test_raise_mailgun_sends_post(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    cfg = AlertConfig(transport="mailgun", smtp=None, mailgun=_mailgun_config())

    with patch("door_sync.alert.httpx.post") as mock_post:
        alert.raise_("safety halt", path=path, alert_config=cfg)

    mock_post.assert_called_once()
    # The Mailgun response status must be checked (a 4xx/5xx should surface as a
    # send failure, not a silent success).
    mock_post.return_value.raise_for_status.assert_called_once()
    call_kwargs = mock_post.call_args
    # Assert the exact Mailgun endpoint, not a loose substring: the domain must
    # sit in the API path (https://api.mailgun.net/v3/<domain>/messages), which
    # a substring check would not guarantee.
    assert call_kwargs.args[0] == "https://api.mailgun.net/v3/mg.example.com/messages"
    assert call_kwargs.kwargs["auth"] == ("api", "key-test")
    assert call_kwargs.kwargs["data"]["subject"] == "[door-sync] ALERT"
    assert call_kwargs.kwargs["data"]["text"] == "safety halt"
    assert path.exists()


def test_clear_mailgun_sends_resolved(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    path.write_text("reason\n")
    cfg = AlertConfig(transport="mailgun", smtp=None, mailgun=_mailgun_config())

    with patch("door_sync.alert.httpx.post") as mock_post:
        alert.clear(path=path, alert_config=cfg)

    mock_post.assert_called_once()
    mock_post.return_value.raise_for_status.assert_called_once()
    assert mock_post.call_args.kwargs["data"]["subject"] == "[door-sync] RESOLVED"
    assert not path.exists()


def test_mailgun_failure_does_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "alert.flag"
    cfg = AlertConfig(transport="mailgun", smtp=None, mailgun=_mailgun_config())

    with (
        patch("door_sync.alert.httpx.post", side_effect=ConnectionError("unreachable")),
        caplog.at_level(logging.ERROR, logger="door_sync.alert"),
    ):
        alert.raise_("reason", path=path, alert_config=cfg)

    assert path.exists()
    assert any("failed to send" in r.getMessage() for r in caplog.records)


# --- SMTP transport ---


def _smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.com",
        port=587,
        starttls=True,
        username="user",
        password="pass",
        from_addr="door-sync@example.com",
        to_addrs=("admin@example.com",),
        subject_prefix="[door-sync]",
    )


def test_raise_smtp_sends_email(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    cfg = AlertConfig(transport="smtp", smtp=_smtp_config(), mailgun=None)

    with patch("door_sync.alert.smtplib.SMTP") as mock_cls:
        # mock_cls.return_value is a MagicMock, which already supports the
        # context-manager protocol (and __exit__ returns False, so it won't
        # swallow exceptions). _send_smtp uses `with server:` (no `as`), so no
        # __enter__/__exit__ setup is needed.
        mock_server = mock_cls.return_value
        alert.raise_("safety halt", path=path, alert_config=cfg)

    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user", "pass")
    mock_server.send_message.assert_called_once()
    msg = mock_server.send_message.call_args.args[0]
    assert msg["Subject"] == "[door-sync] ALERT"
    assert path.exists()


def test_clear_smtp_sends_resolved(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    path.write_text("reason\n")
    cfg = AlertConfig(transport="smtp", smtp=_smtp_config(), mailgun=None)

    with patch("door_sync.alert.smtplib.SMTP") as mock_cls:
        # mock_cls.return_value is a MagicMock, which already supports the
        # context-manager protocol (and __exit__ returns False, so it won't
        # swallow exceptions). _send_smtp uses `with server:` (no `as`), so no
        # __enter__/__exit__ setup is needed.
        mock_server = mock_cls.return_value
        alert.clear(path=path, alert_config=cfg)

    msg = mock_server.send_message.call_args.args[0]
    assert msg["Subject"] == "[door-sync] RESOLVED"
    assert not path.exists()


def test_smtp_failure_does_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "alert.flag"
    cfg = AlertConfig(transport="smtp", smtp=_smtp_config(), mailgun=None)

    with (
        patch(
            "door_sync.alert.smtplib.SMTP",
            side_effect=ConnectionError("refused"),
        ),
        caplog.at_level(logging.ERROR, logger="door_sync.alert"),
    ):
        alert.raise_("reason", path=path, alert_config=cfg)

    assert path.exists()
    assert any("failed to send" in r.getMessage() for r in caplog.records)


def test_smtp_ssl_used_when_starttls_false(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    smtp_cfg = SmtpConfig(
        host="smtp.example.com",
        port=465,
        starttls=False,
        username="user",
        password="pass",
        from_addr="door-sync@example.com",
        to_addrs=("admin@example.com",),
        subject_prefix="[door-sync]",
    )
    cfg = AlertConfig(transport="smtp", smtp=smtp_cfg, mailgun=None)

    with patch("door_sync.alert.smtplib.SMTP_SSL") as mock_cls:
        # mock_cls.return_value is a MagicMock, which already supports the
        # context-manager protocol (and __exit__ returns False, so it won't
        # swallow exceptions). _send_smtp uses `with server:` (no `as`), so no
        # __enter__/__exit__ setup is needed.
        mock_server = mock_cls.return_value
        alert.raise_("reason", path=path, alert_config=cfg)

    mock_cls.assert_called_once()
    mock_server.login.assert_called_once()


# --- alert_config=None backwards compat ---


def test_raise_with_none_alert_config_only_writes_flag(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    alert.raise_("reason", path=path, alert_config=None)
    assert path.exists()


def test_clear_with_none_alert_config_only_removes_flag(tmp_path: Path) -> None:
    path = tmp_path / "alert.flag"
    path.write_text("reason\n")
    alert.clear(path=path, alert_config=None)
    assert not path.exists()
