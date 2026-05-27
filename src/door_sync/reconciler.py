"""Pure diff computation between resolved CiviCRM members and UniFi users.

No I/O, no logging, no exceptions on data issues. Takes dataclasses,
returns a Diff dataclass.
"""

from door_sync.models import Diff, ResolvedMember, UnifiUser


def compute_diff(resolved: list[ResolvedMember], unifi: list[UnifiUser]) -> Diff:
    """Compute the set of changes needed to reconcile UniFi with CiviCRM.

    Args:
        resolved: Tier-mapped CiviCRM members (source of truth).
        unifi: Current UniFi Access users.

    Returns:
        A `Diff` describing additions, updates, deactivations, and unmapped members.
    """
    resolved_by_id: dict[int, ResolvedMember] = {r.contact_id: r for r in resolved}
    unifi_by_id: dict[int, UnifiUser] = {u.contact_id: u for u in unifi}

    to_add: list[ResolvedMember] = []
    to_update_credential: list[tuple[ResolvedMember, UnifiUser]] = []
    to_update_policy: list[tuple[ResolvedMember, UnifiUser]] = []
    to_deactivate: list[UnifiUser] = []
    unmapped: list[ResolvedMember] = []

    all_ids = set(resolved_by_id.keys()) | set(unifi_by_id.keys())

    for cid in all_ids:
        r = resolved_by_id.get(cid)
        u = unifi_by_id.get(cid)

        if r is None:
            # Contact only in UniFi: deactivate if currently active.
            # u cannot be None here — all_ids is the union of both dicts.
            if u is not None and u.active:
                to_deactivate.append(u)
            continue

        if r.resolution == "unmapped":
            unmapped.append(r)
            continue

        if r.resolution == "day-pass":
            # Never touch day-pass resolutions, regardless of UniFi state.
            continue

        if r.resolution == "none":
            if u is not None and u.active:
                to_deactivate.append(u)
            continue

        # r.resolution == "tier"
        if r.target_policy is None:
            # Malformed input — tier_mapping should never produce this.
            # Treat as no-op rather than raising (pure modules don't raise).
            continue

        if u is None or not u.active:
            to_add.append(r)
            continue

        # u present + active + tier resolution
        cred_changed = u.card_id != r.card_id or u.display_name != r.display_name
        pol_changed = u.policy != r.target_policy

        if cred_changed:
            to_update_credential.append((r, u))
        if pol_changed:
            to_update_policy.append((r, u))

    return Diff(
        to_add=tuple(to_add),
        to_update_credential=tuple(to_update_credential),
        to_update_policy=tuple(to_update_policy),
        to_deactivate=tuple(to_deactivate),
        unmapped=tuple(unmapped),
    )
