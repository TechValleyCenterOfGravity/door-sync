from door_sync.models import CiviMember, ResolvedMember, TierMapping, TierRule


def resolve(member: CiviMember, mapping: TierMapping) -> ResolvedMember:
    if not member.membership_types:
        return ResolvedMember(
            contact_id=member.contact_id,
            display_name=member.display_name,
            card_id=member.card_id,
            target_policy=None,
            resolution="unmapped",
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
    )


def resolve_all(
    members: list[CiviMember], mapping: TierMapping
) -> list[ResolvedMember]:
    return [resolve(m, mapping) for m in members]
