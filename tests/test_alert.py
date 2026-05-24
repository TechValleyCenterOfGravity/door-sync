"""Tests for door_sync.alert — flag-file alerting stub."""

import logging
from pathlib import Path

import pytest

from door_sync import alert


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
