from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CiviMember:
    contact_id: int
    display_name: str
    card_id: int | None
    membership_types: list[str]


@dataclass(frozen=True)
class ResolvedMember:
    contact_id: int
    display_name: str
    card_id: int | None
    target_policy: str | None
    resolution: Literal["tier", "none", "day-pass", "unmapped"]


@dataclass(frozen=True)
class UnifiUser:
    contact_id: int
    display_name: str
    card_id: int | None
    active: bool
    policy: str | None


@dataclass(frozen=True)
class Diff:
    to_add: list[ResolvedMember]
    to_update_credential: list[tuple[ResolvedMember, UnifiUser]]
    to_update_policy: list[tuple[ResolvedMember, UnifiUser]]
    to_deactivate: list[UnifiUser]
    unmapped: list[ResolvedMember]


@dataclass(frozen=True)
class CheckResult:
    halted: bool
    reason: str | None


@dataclass(frozen=True)
class ReconcileResult:
    halted: bool
    reason: str | None
    diff: Diff | None


@dataclass(frozen=True)
class TierRule:
    resolution: Literal["tier", "none", "day-pass"]
    target_policy: str | None
    rank: int


@dataclass(frozen=True)
class TierMapping:
    rules: dict[str, TierRule]


@dataclass(frozen=True)
class SafetyThresholds:
    mass_deactivate_pct: float = 0.15
    mass_add_pct: float = 0.25
    mass_policy_pct: float = 0.20
    baseline_floor: int = 10
