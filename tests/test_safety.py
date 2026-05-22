from typing import Literal

from door_sync.models import (
    Diff,
    ResolvedMember,
    SafetyThresholds,
    UnifiUser,
)
from door_sync.safety import check


def _r(
    contact_id: int = 1,
    card_id: int | None = 100,
    target_policy: str | None = "P_GOLD",
    resolution: Literal["tier", "none", "day-pass", "unmapped"] = "tier",
) -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=f"M{contact_id}",
        card_id=card_id,
        target_policy=target_policy,
        resolution=resolution,
    )


def _u(contact_id: int = 1, active: bool = True) -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=f"U{contact_id}",
        card_id=100 + contact_id,
        active=active,
        policy="P_GOLD",
    )


def _empty_diff() -> Diff:
    return Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )


def _high_baseline() -> int:
    # Comfortably above default floor (10)
    return 100


def test_clean_diff_not_halted() -> None:
    result = check(_empty_diff(), baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is False
    assert result.reason is None


# --- Guard 1: unmapped types ---


def test_unmapped_non_empty_halts() -> None:
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[_r(resolution="unmapped", target_policy=None)],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert result.reason is not None
    assert "unmapped" in result.reason.lower()


# --- Guard 2: duplicate card IDs ---


def test_duplicate_card_in_to_add_halts() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=42), _r(contact_id=2, card_id=42)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert result.reason is not None
    assert "duplicate" in result.reason.lower()


def test_duplicate_card_across_add_and_update_credential_halts() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=42)],
        to_update_credential=[(_r(contact_id=2, card_id=42), _u(contact_id=2))],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert "duplicate" in (result.reason or "").lower()


def test_none_card_ids_dont_count_as_duplicates() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=None), _r(contact_id=2, card_id=None)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is False


# --- Guard 3: invalid card ID ---


def test_negative_card_id_halts() -> None:
    diff = Diff(
        to_add=[_r(card_id=-1)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert "invalid" in (result.reason or "").lower()


def test_card_id_above_65535_halts() -> None:
    diff = Diff(
        to_add=[_r(card_id=70000)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert "invalid" in (result.reason or "").lower()


def test_card_id_at_boundary_0_and_65535_is_valid() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=0), _r(contact_id=2, card_id=65535)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is False


# --- Guard 4: mass deactivation ---


def test_mass_deactivation_just_over_threshold_halts() -> None:
    # 16 / 100 = 16% > 15%
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_u(contact_id=i) for i in range(16)],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "deactivat" in (result.reason or "").lower()


def test_mass_deactivation_just_under_threshold_does_not_halt() -> None:
    # 15 / 100 = 15% not > 15%
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_u(contact_id=i) for i in range(15)],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is False


# --- Guard 5: mass addition ---


def test_mass_addition_over_threshold_halts() -> None:
    # 26 / 100 = 26% > 25%, with unique card ids to avoid tripping dup guard
    diff = Diff(
        to_add=[_r(contact_id=i, card_id=1000 + i) for i in range(26)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "addition" in (result.reason or "").lower() or "add" in (result.reason or "").lower()


# --- Guard 6: mass policy change ---


def test_mass_policy_change_over_threshold_halts() -> None:
    # 21 / 100 = 21% > 20%
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[
            (_r(contact_id=i, card_id=2000 + i), _u(contact_id=i))
            for i in range(21)
        ],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "policy" in (result.reason or "").lower()


# --- Baseline floor behavior ---


def test_mass_guards_skipped_when_baseline_below_floor() -> None:
    # 5 to_deactivate, baseline=5 → would be 100%, way over 15%
    # But baseline=5 < floor=10, so guard skipped.
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_u(contact_id=i) for i in range(5)],
        unmapped=[],
    )
    result = check(diff, baseline=5, thresholds=SafetyThresholds())
    assert result.halted is False


def test_integrity_guards_run_even_below_floor() -> None:
    # Below floor, but unmapped still trips.
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[_r(resolution="unmapped", target_policy=None)],
    )
    result = check(diff, baseline=5, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "unmapped" in (result.reason or "").lower()
