"""Pure tier-mapping resolution for CiviCRM members.

Maps each member's active membership types to a target access policy
using ranked rules from configuration. No I/O, no logging.
"""

from door_sync.models import CiviMember, ResolvedMember, TierMapping, TierRule


def resolve(member: CiviMember, mapping: TierMapping) -> ResolvedMember:
    """Resolve a single member's membership types to a target policy.

    Args:
        member: CiviCRM member with active membership types.
        mapping: Tier-mapping rules from configuration.

    Returns:
        A `ResolvedMember` with the resolution outcome and target policy.
    """
    if not member.membership_types:
        return ResolvedMember(
            contact_id=member.contact_id,
            display_name=member.display_name,
            card_id=member.card_id,
            target_policy=None,
            resolution="none",
            email=member.email,
        )

    matched: list[tuple[str, TierRule]] = []
    for type_name in member.membership_types:
        rule = mapping.rules.get(type_name)
        if rule is None:
            return ResolvedMember(
                contact_id=member.contact_id,
                display_name=member.display_name,
                card_id=member.card_id,
                target_policy=None,
                resolution="unmapped",
                email=member.email,
            )
        matched.append((type_name, rule))

    # Highest-wins: sort by rank desc, then by type name asc; take the first.
    matched.sort(key=lambda t: (-t[1].rank, t[0]))
    chosen = matched[0][1]
    return ResolvedMember(
        contact_id=member.contact_id,
        display_name=member.display_name,
        card_id=member.card_id,
        target_policy=chosen.target_policy,
        resolution=chosen.resolution,
        email=member.email,
    )


def managed_policy_ids(mapping: TierMapping) -> frozenset[str]:
    """Collect the set of access policy IDs door-sync manages.

    These are every tier rule's ``target_policy``; ``none``/``day-pass`` rules
    carry no policy. The UniFi client uses this set to distinguish the policy it
    assigned from policies applied externally (e.g. a policy auto-applied to all
    users), so the latter are neither mistaken for a tier change nor stripped on
    write.

    Args:
        mapping: Tier-mapping rules from configuration.

    Returns:
        The frozenset of managed (tier) policy IDs.
    """
    return frozenset(
        rule.target_policy
        for rule in mapping.rules.values()
        if rule.resolution == "tier" and rule.target_policy is not None
    )


def resolve_all(members: list[CiviMember], mapping: TierMapping) -> list[ResolvedMember]:
    """Resolve all members in a batch.

    Args:
        members: CiviCRM members to resolve.
        mapping: Tier-mapping rules from configuration.

    Returns:
        List of resolved members in the same order as input.
    """
    return [resolve(m, mapping) for m in members]
