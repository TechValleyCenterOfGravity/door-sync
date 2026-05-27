"""Tests for door_sync.orchestrator — full reconcile cycle integration.

Uses inline FakeCivicrmClient and FakeUnifiClient (plain Python classes
matching the real clients' surface). No unittest.mock — the fakes
document what orchestrator.reconcile actually depends on.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from door_sync import orchestrator
from door_sync.config import (
    AlertConfig,
    CivicrmConfig,
    Config,
    OpsPaths,
    UnifiConfig,
)
from door_sync.models import (
    CiviMember,
    Diff,
    SafetyThresholds,
    TierMapping,
    TierRule,
    UnifiUser,
)


class FakeCivicrmClient:
    """Matches CivicrmClient surface: fetch_active() + context manager."""

    def __init__(
        self,
        cfg: CivicrmConfig,
        *,
        members: list[CiviMember] | None = None,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._members = members or []
        self._raise = raise_on_fetch

    def fetch_active(self) -> list[CiviMember]:
        if self._raise is not None:
            raise self._raise
        return self._members

    def __enter__(self) -> "FakeCivicrmClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class FakeUnifiClient:
    """Matches UnifiClient surface: fetch_users(), apply(diff), dry_run flag."""

    def __init__(
        self,
        cfg: UnifiConfig,
        *,
        dry_run: bool = False,
        users: list[UnifiUser] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self._users = list(users or [])
        self.apply_calls: list[Diff] = []
        self._fetch_called = False

    def fetch_users(self) -> list[UnifiUser]:
        self._fetch_called = True
        return list(self._users)

    def apply(self, diff: Diff) -> None:
        if not self._fetch_called:
            raise RuntimeError("apply() requires prior fetch_users() call")
        self.apply_calls.append(diff)
        if self.dry_run:
            return
        # Mutate the in-memory store so a follow-up fetch reflects the writes
        # (used by the idempotency canary test).
        by_contact = {u.contact_id: u for u in self._users}
        for m in diff.to_add:
            by_contact[m.contact_id] = UnifiUser(
                contact_id=m.contact_id,
                display_name=m.display_name,
                card_id=m.card_id,
                active=True,
                policy=m.target_policy,
            )

        def _require(contact_id: int, op: str) -> UnifiUser:
            user = by_contact.get(contact_id)
            if user is None:
                raise AssertionError(
                    f"FakeUnifiClient.apply: {op} requested for contact "
                    f"{contact_id} not present in fake user store"
                )
            return user

        for m, _ in diff.to_update_credential:
            existing = _require(m.contact_id, "to_update_credential")
            by_contact[m.contact_id] = UnifiUser(
                contact_id=existing.contact_id,
                display_name=m.display_name,
                card_id=m.card_id,
                active=existing.active,
                policy=existing.policy,
            )
        for m, _ in diff.to_update_policy:
            existing = _require(m.contact_id, "to_update_policy")
            by_contact[m.contact_id] = UnifiUser(
                contact_id=existing.contact_id,
                display_name=existing.display_name,
                card_id=existing.card_id,
                active=existing.active,
                policy=m.target_policy,
            )
        for u in diff.to_deactivate:
            existing = _require(u.contact_id, "to_deactivate")
            by_contact[u.contact_id] = UnifiUser(
                contact_id=existing.contact_id,
                display_name=existing.display_name,
                card_id=existing.card_id,
                active=False,
                policy=existing.policy,
            )
        self._users = list(by_contact.values())

    def __enter__(self) -> "FakeUnifiClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _config(tmp_path: Path) -> Config:
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
        alert=AlertConfig(transport="flag-file", smtp=None, mailgun=None),
    )


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    civi_members: list[CiviMember],
    unifi_users: list[UnifiUser],
    civi_raise: Exception | None = None,
) -> dict[str, Any]:
    """Patch CivicrmClient and UnifiClient symbols in orchestrator namespace."""
    holder: dict[str, Any] = {}

    def make_civi(cfg: CivicrmConfig) -> FakeCivicrmClient:
        client = FakeCivicrmClient(cfg, members=civi_members, raise_on_fetch=civi_raise)
        holder["civi"] = client
        return client

    def make_unifi(cfg: UnifiConfig, *, dry_run: bool = False) -> FakeUnifiClient:
        client = FakeUnifiClient(cfg, dry_run=dry_run, users=unifi_users)
        holder["unifi"] = client
        return client

    monkeypatch.setattr(orchestrator, "CivicrmClient", make_civi)
    monkeypatch.setattr(orchestrator, "UnifiClient", make_unifi)
    return holder


def test_happy_path_no_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    # 12 baseline active users (above SafetyThresholds.baseline_floor=10)
    members = [
        CiviMember(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, membership_types=("Gold",)
        )
        for i in range(1, 13)
    ]
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 13)
    ]
    _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is False
    assert result.diff is not None
    assert result.diff.to_add == ()
    assert result.diff.to_deactivate == ()

    # Audit: one applied line
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["event"] == "applied"

    # State: last_success_iso populated; alert flag absent
    assert (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()


def test_apply_with_drift_calls_unifi_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    members = [
        CiviMember(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, membership_types=("Gold",)
        )
        for i in range(1, 12)
    ]
    members.append(  # new member not yet in UniFi
        CiviMember(contact_id=99, display_name="New", card_id=0x9999, membership_types=("Gold",))
    )
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 12)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is False
    unifi: FakeUnifiClient = holder["unifi"]
    assert len(unifi.apply_calls) == 1
    assert len(unifi.apply_calls[0].to_add) == 1
    assert unifi.apply_calls[0].to_add[0].contact_id == 99


def test_safety_halt_writes_alert_flag_and_audit_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    # 20 users in UniFi; CiviCRM returns only 10 — would deactivate 50% > 15% threshold
    members = [
        CiviMember(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, membership_types=("Gold",)
        )
        for i in range(1, 11)
    ]
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 21)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is True
    assert result.reason is not None
    unifi: FakeUnifiClient = holder["unifi"]
    assert unifi.apply_calls == []  # never applied

    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    halted = json.loads(audit_lines[0])
    assert halted["event"] == "halted"
    assert halted["reason"] == result.reason

    flag = tmp_path / "alert.flag"
    assert flag.exists()
    assert result.reason in flag.read_text()


def test_idempotency_canary_second_cycle_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After applying drift, the next compute_diff (with the same data) must
    yield empty diff sets — architecture §8."""
    cfg = _config(tmp_path)
    members = [
        CiviMember(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, membership_types=("Gold",)
        )
        for i in range(1, 13)
    ]
    members.append(
        CiviMember(contact_id=99, display_name="New", card_id=0x9999, membership_types=("Gold",))
    )
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 13)
    ]

    # For the idempotency test, reuse the SAME FakeUnifiClient so its
    # mutated _users store is visible on the second cycle.
    shared_unifi: dict[str, FakeUnifiClient] = {}

    def make_civi(cfg: CivicrmConfig) -> FakeCivicrmClient:
        return FakeCivicrmClient(cfg, members=members)

    def make_unifi(cfg: UnifiConfig, *, dry_run: bool = False) -> FakeUnifiClient:
        if "client" not in shared_unifi:
            shared_unifi["client"] = FakeUnifiClient(cfg, dry_run=dry_run, users=users)
        return shared_unifi["client"]

    monkeypatch.setattr(orchestrator, "CivicrmClient", make_civi)
    monkeypatch.setattr(orchestrator, "UnifiClient", make_unifi)

    # First cycle: apply
    first = orchestrator.reconcile(cfg, dry_run=False)
    assert first.halted is False
    assert first.diff is not None
    assert len(first.diff.to_add) == 1

    # Second cycle: the same FakeUnifiClient (held in monkeypatched factory)
    # should now have the new user in its store, so diff is empty.
    second = orchestrator.reconcile(cfg, dry_run=False)
    assert second.halted is False
    assert second.diff is not None
    assert second.diff.to_add == ()
    assert second.diff.to_update_credential == ()
    assert second.diff.to_update_policy == ()
    assert second.diff.to_deactivate == ()


def test_dry_run_apply_does_not_touch_state_or_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    members = [
        CiviMember(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, membership_types=("Gold",)
        )
        for i in range(1, 13)
    ]
    members.append(
        CiviMember(contact_id=99, display_name="New", card_id=0x9999, membership_types=("Gold",))
    )
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 13)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=True)

    assert result.halted is False
    unifi: FakeUnifiClient = holder["unifi"]
    assert unifi.dry_run is True
    # apply was still called; dry_run guarding lives inside UnifiClient
    assert len(unifi.apply_calls) == 1

    audit_line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit_line["dry_run"] is True

    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()


def test_dry_run_halt_writes_alert_flag_but_not_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    members = [
        CiviMember(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, membership_types=("Gold",)
        )
        for i in range(1, 11)
    ]
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 21)
    ]
    _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    result = orchestrator.reconcile(cfg, dry_run=True)

    assert result.halted is True

    audit_line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit_line["event"] == "halted"
    assert audit_line["dry_run"] is True

    assert (tmp_path / "alert.flag").exists()  # halt still flags
    assert not (tmp_path / "state.json").exists()  # but dry-run never touches state


def test_civicrm_exception_propagates_with_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    _patch_clients(
        monkeypatch,
        civi_members=[],
        unifi_users=[],
        civi_raise=ConnectionError("DNS lookup failed"),
    )

    with pytest.raises(ConnectionError):
        orchestrator.reconcile(cfg, dry_run=False)

    # Orchestrator does not catch — so no audit/state/alert touched by it.
    assert not (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "alert.flag").exists()


def test_handle_crash_logs_audits_and_raises_alert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config(tmp_path)
    exc = ConnectionError("boom")

    with caplog.at_level(logging.ERROR, logger="door_sync.orchestrator"):
        orchestrator.handle_crash(exc, paths=cfg.ops_paths)

    audit_line = json.loads(cfg.ops_paths.audit_jsonl.read_text().splitlines()[0])
    assert audit_line["event"] == "crashed"
    assert audit_line["exception"]["class"] == "ConnectionError"
    assert audit_line["exception"]["message"] == "boom"

    assert cfg.ops_paths.alert_flag.exists()
    flag_text = cfg.ops_paths.alert_flag.read_text()
    assert "crashed" in flag_text
    assert "ConnectionError" in flag_text
    assert "boom" in flag_text


def test_handle_crash_truncates_long_exception_messages(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    long_msg = "x" * 500
    orchestrator.handle_crash(RuntimeError(long_msg), paths=cfg.ops_paths)

    flag_text = cfg.ops_paths.alert_flag.read_text()
    # Truncation matches __main__'s current 200-char + "..." behavior.
    assert "..." in flag_text
    assert "x" * 200 in flag_text
    assert "x" * 201 not in flag_text
