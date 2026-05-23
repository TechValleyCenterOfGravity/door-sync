"""Tests for door_sync.__main__ — CLI entry point.

Calls main(argv=[...]) directly with monkeypatched orchestrator.reconcile,
so the full subprocess is not needed.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from door_sync import __main__ as main_mod
from door_sync import orchestrator
from door_sync.config import (
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    OpsPaths,
    UnifiConfig,
)
from door_sync.models import (
    Diff,
    ReconcileResult,
    SafetyThresholds,
    TierMapping,
    TierRule,
)


def _build_config(tmp_path: Path) -> Config:
    return Config(
        cadence_seconds=600,
        civicrm=CivicrmConfig(
            host="https://civicrm.example.org",
            api_key="k",
            card_id_field="Door_Access.card_id",
        ),
        unifi=UnifiConfig(
            host="https://unifi.example.org:12445",
            api_key="k",
            tls_fingerprint="AB:" * 31 + "AB",
            facility_code=42,
        ),
        safety=SafetyThresholds(),
        tier_mapping=TierMapping(
            rules={"Gold": TierRule(resolution="tier", target_policy="p1", rank=100)}
        ),
        ops_paths=OpsPaths(
            audit_jsonl=tmp_path / "audit.jsonl",
            state_json=tmp_path / "state.json",
            alert_flag=tmp_path / "alert.flag",
        ),
    )


def _patch_config_load(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr(main_mod.config_mod, "load", lambda **_: cfg)


def test_run_once_success_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    monkeypatch.setattr(
        orchestrator,
        "reconcile",
        lambda c, *, dry_run: ReconcileResult(
            halted=False, reason=None, diff=Diff([], [], [], [], [])
        ),
    )

    rc = main_mod.main(argv=["run", "--once"])
    assert rc == 0


def test_run_once_halt_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    monkeypatch.setattr(
        orchestrator,
        "reconcile",
        lambda c, *, dry_run: ReconcileResult(
            halted=True,
            reason="mass_deactivate",
            diff=Diff([], [], [], [], []),
        ),
    )

    rc = main_mod.main(argv=["run", "--once"])
    assert rc == 1


def test_run_once_crash_writes_audit_alert_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)

    def _raise(c: Config, *, dry_run: bool) -> ReconcileResult:
        raise ConnectionError("boom")

    monkeypatch.setattr(orchestrator, "reconcile", _raise)

    with caplog.at_level(logging.ERROR):
        rc = main_mod.main(argv=["run", "--once"])

    assert rc == 2

    audit_line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit_line["event"] == "crashed"
    assert audit_line["exception"]["class"] == "ConnectionError"

    flag = tmp_path / "alert.flag"
    assert flag.exists()
    assert "boom" in flag.read_text()


def test_run_without_once_returns_64(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main_mod.main(argv=["run"])
    assert rc == 64
    captured = capsys.readouterr()
    assert "daemon mode not yet implemented" in captured.err


def test_validate_config_bad_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _bad(**_: Any) -> Config:
        raise ConfigError(
            [ConfigIssue(path="unifi.host", message="must start with https://")]
        )

    monkeypatch.setattr(main_mod.config_mod, "load", _bad)

    rc = main_mod.main(argv=["validate-config"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unifi.host" in captured.err
    assert "must start with https://" in captured.err


def test_validate_config_good_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config_load(monkeypatch, _build_config(tmp_path))

    rc = main_mod.main(argv=["validate-config"])
    assert rc == 0


def test_show_diff_prints_sections_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)

    # Patch the clients inside __main__'s show_diff path. Simplest: patch
    # main_mod.CivicrmClient and main_mod.UnifiClient — show_diff
    # imports them through __main__.
    class _Civi:
        def __init__(self, c: CivicrmConfig) -> None:
            pass

        def fetch_active(self) -> list[Any]:
            return []

        def __enter__(self) -> "_Civi":
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    class _Unifi:
        def __init__(self, c: UnifiConfig, *, dry_run: bool = False) -> None:
            pass

        def fetch_users(self) -> list[Any]:
            return []

        def __enter__(self) -> "_Unifi":
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    monkeypatch.setattr(main_mod, "CivicrmClient", _Civi)
    monkeypatch.setattr(main_mod, "UnifiClient", _Unifi)

    rc = main_mod.main(argv=["show-diff"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "=== ADD (0) ===" in captured.out
    assert "=== DEACTIVATE (0) ===" in captured.out

    # show-diff must NOT touch audit/state/alert
    assert not (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()
