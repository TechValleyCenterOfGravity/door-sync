from door_sync.models import CiviMember, ResolvedMember, TierMapping, TierRule
from door_sync.tier_mapping import resolve, resolve_all


def _civi(types: list[str]) -> CiviMember:
    return CiviMember(
        contact_id=1, display_name="A", card_id=42, membership_types=types
    )


def test_single_tier_match() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(["Gold"]), mapping)
    assert result.resolution == "tier"
    assert result.target_policy == "P_GOLD"
    assert result.contact_id == 1
    assert result.display_name == "A"
    assert result.card_id == 42


def test_single_none_match() -> None:
    mapping = TierMapping(
        rules={"Comp": TierRule(resolution="none", target_policy=None, rank=1)}
    )
    result = resolve(_civi(["Comp"]), mapping)
    assert result.resolution == "none"
    assert result.target_policy is None


def test_single_day_pass_match() -> None:
    mapping = TierMapping(
        rules={"DayPass": TierRule(resolution="day-pass", target_policy=None, rank=1)}
    )
    result = resolve(_civi(["DayPass"]), mapping)
    assert result.resolution == "day-pass"
    assert result.target_policy is None


def test_unmapped_when_type_missing() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(["Silver"]), mapping)
    assert result.resolution == "unmapped"
    assert result.target_policy is None


def test_unmapped_when_membership_types_empty() -> None:
    mapping = TierMapping(rules={})
    result = resolve(_civi([]), mapping)
    assert result.resolution == "unmapped"
    assert result.target_policy is None


def test_highest_wins_two_tier_rules() -> None:
    mapping = TierMapping(
        rules={
            "Silver": TierRule(resolution="tier", target_policy="P_SILVER", rank=5),
            "Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10),
        }
    )
    result = resolve(_civi(["Silver", "Gold"]), mapping)
    assert result.target_policy == "P_GOLD"


def test_highest_wins_across_resolution_kinds() -> None:
    mapping = TierMapping(
        rules={
            "Comp": TierRule(resolution="none", target_policy=None, rank=99),
            "Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10),
        }
    )
    result = resolve(_civi(["Comp", "Gold"]), mapping)
    # Comp has higher rank, so its resolution wins (even though it's "none")
    assert result.resolution == "none"
    assert result.target_policy is None


def test_mixed_matched_and_unmatched_is_unmapped() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(["Gold", "MysteryType"]), mapping)
    # Fail-secure beats highest-wins
    assert result.resolution == "unmapped"


def test_tie_on_rank_resolves_deterministically() -> None:
    # Two rules at rank 5 — type name asc, so "A" beats "B"
    mapping = TierMapping(
        rules={
            "B_Type": TierRule(resolution="tier", target_policy="P_B", rank=5),
            "A_Type": TierRule(resolution="tier", target_policy="P_A", rank=5),
        }
    )
    result = resolve(_civi(["B_Type", "A_Type"]), mapping)
    assert result.target_policy == "P_A"


def test_resolve_all_preserves_order() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    members = [
        CiviMember(contact_id=i, display_name=f"M{i}", card_id=i, membership_types=["Gold"])
        for i in (1, 2, 3)
    ]
    results = resolve_all(members, mapping)
    assert [r.contact_id for r in results] == [1, 2, 3]
    assert all(isinstance(r, ResolvedMember) for r in results)
