"""Frozen domain dataclasses shared across all door-sync modules.

These are the data contracts that flow between pure and impure layers.
All classes are frozen (immutable) — construct new instances instead of mutating.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CiviMember:
    """A CiviCRM contact with a card ID and their active membership types.

    Parameters:
        contact_id: CiviCRM contact primary key.
        display_name: Contact's display name from CiviCRM.
        card_id: Wiegand-26 card number, or None if not provisioned.
        membership_types: Active membership type labels (Current/Grace status).
        email: Contact's primary email from CiviCRM, or None if absent.
    """

    contact_id: int
    display_name: str
    card_id: int | None
    membership_types: tuple[str, ...]
    email: str | None = None


@dataclass(frozen=True)
class ResolvedMember:
    """A CiviMember after tier-mapping resolution.

    Parameters:
        contact_id: CiviCRM contact primary key.
        display_name: Contact's display name from CiviCRM.
        card_id: Wiegand-26 card number, or None if not provisioned.
        target_policy: UniFi access policy ID to assign, or None.
        resolution: Outcome of tier mapping: tier, none, day-pass, or unmapped.
        email: Contact's primary email carried through from CiviCRM, or None.
    """

    contact_id: int
    display_name: str
    card_id: int | None
    target_policy: str | None
    resolution: Literal["tier", "none", "day-pass", "unmapped"]
    email: str | None = None


@dataclass(frozen=True)
class UnifiUser:
    """A user record from the UniFi Access controller.

    Parameters:
        contact_id: CiviCRM contact ID (stored as employee_number in UniFi).
        display_name: User's display name in UniFi.
        card_id: Wiegand-26 card number parsed from NFC ID, or None.
        active: Whether the user's UniFi status is ACTIVE.
        policy: Current access policy ID, or None if unassigned.
        email: User's email (user_email) as read from UniFi, or None.
    """

    contact_id: int
    display_name: str
    card_id: int | None
    active: bool
    policy: str | None
    email: str | None = None


@dataclass(frozen=True)
class Diff:
    """Computed difference between resolved CiviCRM state and UniFi state.

    Parameters:
        to_add: Members to provision as new UniFi users.
        to_update_credential: Members whose card ID or display name changed, paired with current UniFi state.
        to_update_policy: Members whose access policy changed, paired with current UniFi state.
        to_deactivate: UniFi users to deactivate (no longer in CiviCRM or resolved to 'none').
        unmapped: Members with membership types that have no matching tier rule.
    """

    to_add: tuple[ResolvedMember, ...]
    to_update_credential: tuple[tuple[ResolvedMember, UnifiUser], ...]
    to_update_policy: tuple[tuple[ResolvedMember, UnifiUser], ...]
    to_deactivate: tuple[UnifiUser, ...]
    unmapped: tuple[ResolvedMember, ...]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of safety-guard evaluation on a Diff.

    Parameters:
        halted: True if any guard fired and the cycle must not proceed.
        reason: Human-readable explanation when halted, None otherwise.
    """

    halted: bool
    reason: str | None


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a single reconciliation cycle.

    Parameters:
        halted: True if safety guards prevented application.
        reason: Halt reason when halted, None otherwise.
        diff: The computed diff, or None if the cycle crashed before diffing.
    """

    halted: bool
    reason: str | None
    diff: Diff | None


@dataclass(frozen=True)
class TierRule:
    """A single tier-mapping rule from configuration.

    Parameters:
        resolution: How to handle members matching this rule: tier, none, or day-pass.
        target_policy: UniFi access policy ID when resolution is 'tier', None otherwise.
        rank: Priority rank for multi-membership resolution (highest wins).
    """

    resolution: Literal["tier", "none", "day-pass"]
    target_policy: str | None
    rank: int


@dataclass(frozen=True)
class TierMapping:
    """Complete tier-mapping configuration keyed by CiviCRM membership type label.

    Parameters:
        rules: Mapping from membership type name to its TierRule.
    """

    rules: dict[str, TierRule]


@dataclass(frozen=True)
class SafetyThresholds:
    """Configurable thresholds for mass-change safety guards.

    Parameters:
        mass_deactivate_pct: Max fraction of active users that may be deactivated per cycle.
        mass_add_pct: Max fraction of active baseline that may be added per cycle.
        mass_policy_pct: Max fraction of active users whose policy may change per cycle.
        baseline_floor: Minimum active-user count before percentage guards engage.
    """

    mass_deactivate_pct: float = 0.15
    mass_add_pct: float = 0.25
    mass_policy_pct: float = 0.20
    baseline_floor: int = 10


@dataclass(frozen=True)
class State:
    """Persistent state written to disk between reconciliation cycles.

    Parameters:
        last_success_iso: ISO 8601 timestamp of the last successful cycle, or None.
        last_halt_iso: ISO 8601 timestamp of the last halted cycle, or None.
        last_halt_reason: Reason for the last halt, or None.
        run_count: Monotonically increasing count of completed cycles.
    """

    last_success_iso: str | None
    last_halt_iso: str | None
    last_halt_reason: str | None
    run_count: int
