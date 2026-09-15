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
    AlertConfig,
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    OpsPaths,
    UnifiConfig,
    WebhookConfig,
)
from door_sync.models import (
    Diff,
    ReconcileResult,
    SafetyThresholds,
    TierMapping,
    TierRule,
)


def _build_config(tmp_path: Path, *, webhook_enabled: bool = False) -> Config:
    return Config(
        cadence_seconds=600,
        civicrm=CivicrmConfig(
            host="https://civicrm.example.org",
            api_key="k",
            card_id_field="Door_Access.card_id",
            active_statuses=("Current", "Grace"),
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
        alert=AlertConfig(transport="flag-file", smtp=None, mailgun=None),
        webhook=WebhookConfig(
            enabled=webhook_enabled,
            host="127.0.0.1",
            port=8787,
            hmac_secret="s" * 32 if webhook_enabled else "",
            max_body_bytes=65536,
            max_skew_seconds=300,
            debounce_seconds=0.0,
        ),
    )


def _patch_config_load(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr(main_mod.config_mod, "load", lambda **_: cfg)
    # validate-config also stats the env file for its mode. With load() stubbed
    # there is no real env file to stat, and the resolver would fall back to the
    # developer's own ./.env -- so these CLI tests would depend on the mode of a
    # file outside the repo's control. Permission behaviour is covered directly
    # in test_config.py, and by test_validate_config_flags_permissive_env below.
    monkeypatch.setattr(main_mod.config_mod, "check_env_permissions", lambda _p=None: [])


def test_run_once_success_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)

    def _ok(c: Config, *, dry_run: bool) -> ReconcileResult:
        return ReconcileResult(halted=False, reason=None, diff=Diff((), (), (), (), ()))

    monkeypatch.setattr(orchestrator, "reconcile", _ok)

    rc = main_mod.main(argv=["run", "--once"])
    assert rc == 0


def test_run_once_halt_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)

    def _halt(c: Config, *, dry_run: bool) -> ReconcileResult:
        return ReconcileResult(halted=True, reason="mass_deactivate", diff=Diff((), (), (), (), ()))

    monkeypatch.setattr(orchestrator, "reconcile", _halt)

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


def test_run_daemon_calls_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    recorded: dict[str, object] = {}

    def fake_run_forever(c: Config, *, dry_run: bool, work_queue: object = None) -> int:
        recorded["config"] = c
        recorded["dry_run"] = dry_run
        return 0

    from door_sync import scheduler

    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)

    rc = main_mod.main(argv=["run"])

    assert rc == 0
    assert recorded["config"] is cfg
    assert recorded["dry_run"] is False


def test_run_daemon_with_dry_run_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_config(tmp_path)
    _patch_config_load(monkeypatch, cfg)
    recorded: dict[str, object] = {}

    def fake_run_forever(_c: Config, *, dry_run: bool, work_queue: object = None) -> int:
        recorded["dry_run"] = dry_run
        return 0

    from door_sync import scheduler

    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)

    rc = main_mod.main(argv=["run", "--dry-run"])

    assert rc == 0
    assert recorded["dry_run"] is True


def test_validate_config_bad_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _bad(**_: Any) -> Config:
        raise ConfigError([ConfigIssue(path="unifi.host", message="must start with https://")])

    monkeypatch.setattr(main_mod.config_mod, "load", _bad)

    rc = main_mod.main(argv=["validate-config"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unifi.host" in captured.err
    assert "must start with https://" in captured.err


def test_validate_config_good_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config_load(monkeypatch, _build_config(tmp_path))

    rc = main_mod.main(argv=["validate-config"])
    assert rc == 0


def test_validate_config_flags_permissive_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid config with world-readable secrets still fails the check."""
    monkeypatch.setattr(main_mod.config_mod, "load", lambda **_: _build_config(tmp_path))
    env = tmp_path / "env"
    env.write_text("CIVICRM_API_KEY=x\n")
    env.chmod(0o644)

    # --env-file is a global option: it precedes the subcommand.
    rc = main_mod.main(argv=["--env-file", str(env), "validate-config"])

    assert rc == 1
    assert "0644" in capsys.readouterr().err


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

    captured_kwargs: dict[str, Any] = {}

    class _Unifi:
        def __init__(
            self,
            c: UnifiConfig,
            *,
            dry_run: bool = False,
            managed_policy_ids: frozenset[str] | None = None,
        ) -> None:
            captured_kwargs["managed_policy_ids"] = managed_policy_ids

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

    # show-diff must forward the tier policies as the managed set (cfg maps
    # "Gold" -> "p1"), so an auto-applied global policy isn't read as drift.
    assert captured_kwargs["managed_policy_ids"] == frozenset({"p1"})

    captured = capsys.readouterr()
    assert "=== ADD (0) ===" in captured.out
    assert "=== DEACTIVATE (0) ===" in captured.out

    # show-diff must NOT touch audit/state/alert
    assert not (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()


# --- webhook daemon wiring ---


def _ok_reconcile(c: Config, *, dry_run: bool) -> ReconcileResult:  # noqa: ARG001
    return ReconcileResult(halted=False, reason=None, diff=Diff((), (), (), (), ()))


def test_daemon_starts_and_stops_webhook_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path, webhook_enabled=True)
    _patch_config_load(monkeypatch, cfg)
    recorded: dict[str, object] = {}

    class FakeServer:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self, **_: object) -> None:
            self.stopped = True

    fake_server = FakeServer()

    def fake_start(webhook_config: object, *, work_queue: object) -> FakeServer:
        recorded["start_wcfg"] = webhook_config
        recorded["start_queue"] = work_queue
        return fake_server

    def fake_run_forever(c: Config, *, dry_run: bool, work_queue: object = None) -> int:  # noqa: ARG001
        recorded["run_queue"] = work_queue
        return 0

    from door_sync import scheduler

    monkeypatch.setattr(main_mod.webhook, "start", fake_start)
    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)

    rc = main_mod.main(argv=["run"])

    assert rc == 0
    assert recorded["start_wcfg"] is cfg.webhook
    # The webhook server and the scheduler share the SAME queue object.
    assert recorded["start_queue"] is recorded["run_queue"]
    assert fake_server.stopped is True


def test_daemon_does_not_start_webhook_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _build_config(tmp_path, webhook_enabled=False)
    _patch_config_load(monkeypatch, cfg)
    started: list[object] = []

    def fake_start(*_a: object, **_k: object) -> object:
        started.append(object())
        return object()

    def fake_run_forever(c: Config, *, dry_run: bool, work_queue: object = None) -> int:  # noqa: ARG001
        return 0

    from door_sync import scheduler

    monkeypatch.setattr(main_mod.webhook, "start", fake_start)
    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)

    rc = main_mod.main(argv=["run"])

    assert rc == 0
    assert started == []


def test_run_once_never_starts_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with webhook enabled, --once must not stand up the server.
    cfg = _build_config(tmp_path, webhook_enabled=True)
    _patch_config_load(monkeypatch, cfg)
    started: list[object] = []

    def fake_start(*_a: object, **_k: object) -> object:
        started.append(object())
        return object()

    monkeypatch.setattr(main_mod.webhook, "start", fake_start)
    monkeypatch.setattr(orchestrator, "reconcile", _ok_reconcile)

    rc = main_mod.main(argv=["run", "--once"])

    assert rc == 0
    assert started == []
