from dataclasses import FrozenInstanceError

import pytest

from door_sync.models import (
    CheckResult,
    CiviMember,
    Diff,
    ReconcileResult,
    ResolvedMember,
    SafetyThresholds,
    TierMapping,
    TierRule,
    UnifiUser,
)


def test_civi_member_is_frozen() -> None:
    m = CiviMember(contact_id=1, display_name="A", card_id=None, membership_types=())
    with pytest.raises(FrozenInstanceError):
        m.contact_id = 2  # type: ignore[misc]


def test_resolved_member_is_frozen() -> None:
    r = ResolvedMember(
        contact_id=1,
        display_name="A",
        card_id=None,
        target_policy=None,
        resolution="unmapped",
    )
    with pytest.raises(FrozenInstanceError):
        r.resolution = "tier"  # type: ignore[misc]


def test_unifi_user_is_frozen() -> None:
    u = UnifiUser(contact_id=1, display_name="A", card_id=None, active=True, policy=None)
    with pytest.raises(FrozenInstanceError):
        u.active = False  # type: ignore[misc]


def test_diff_is_frozen() -> None:
    d = Diff(
        to_add=(),
        to_update_credential=(),
        to_update_policy=(),
        to_deactivate=(),
        unmapped=(),
    )
    with pytest.raises(FrozenInstanceError):
        d.to_add = []  # type: ignore[misc]


def test_check_result_is_frozen() -> None:
    c = CheckResult(halted=False, reason=None)
    with pytest.raises(FrozenInstanceError):
        c.halted = True  # type: ignore[misc]


def test_reconcile_result_is_frozen() -> None:
    rr = ReconcileResult(halted=False, reason=None, diff=None)
    with pytest.raises(FrozenInstanceError):
        rr.halted = True  # type: ignore[misc]


def test_tier_rule_is_frozen() -> None:
    t = TierRule(resolution="tier", target_policy="P1", rank=1)
    with pytest.raises(FrozenInstanceError):
        t.rank = 2  # type: ignore[misc]


def test_tier_mapping_is_frozen() -> None:
    m = TierMapping(rules={})
    with pytest.raises(FrozenInstanceError):
        m.rules = {}  # type: ignore[misc]


def test_safety_thresholds_defaults() -> None:
    t = SafetyThresholds()
    assert t.mass_deactivate_pct == 0.15
    assert t.mass_add_pct == 0.25
    assert t.mass_policy_pct == 0.20
    assert t.baseline_floor == 10


def test_safety_thresholds_is_frozen() -> None:
    t = SafetyThresholds()
    with pytest.raises(FrozenInstanceError):
        t.mass_add_pct = 0.5  # type: ignore[misc]


def test_dataclass_equality_round_trips() -> None:
    a = CiviMember(contact_id=1, display_name="A", card_id=42, membership_types=("X",))
    b = CiviMember(contact_id=1, display_name="A", card_id=42, membership_types=("X",))
    assert a == b


def test_civi_member_email_defaults_none_and_accepts_value() -> None:
    m = CiviMember(contact_id=1, display_name="A", card_id=10, membership_types=("Gold",))
    assert m.email is None

    m2 = CiviMember(
        contact_id=1,
        display_name="A",
        card_id=10,
        membership_types=("Gold",),
        email="a@example.com",
    )
    assert m2.email == "a@example.com"


def test_resolved_member_email_defaults_none_and_accepts_value() -> None:
    r = ResolvedMember(
        contact_id=1, display_name="A", card_id=10, target_policy="P", resolution="tier"
    )
    assert r.email is None
    r2 = ResolvedMember(
        contact_id=1,
        display_name="A",
        card_id=10,
        target_policy="P",
        resolution="tier",
        email="a@example.com",
    )
    assert r2.email == "a@example.com"


def test_unifi_user_email_defaults_none_and_accepts_value() -> None:
    u = UnifiUser(contact_id=1, display_name="A", card_id=10, active=True, policy="P")
    assert u.email is None
    u2 = UnifiUser(
        contact_id=1, display_name="A", card_id=10, active=True, policy="P", email="a@example.com"
    )
    assert u2.email == "a@example.com"
