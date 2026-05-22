from door_sync.models import CheckResult, Diff, SafetyThresholds


def check(
    diff: Diff,
    *,
    baseline: int,
    thresholds: SafetyThresholds,
) -> CheckResult:
    # Guard 1: unmapped types
    if diff.unmapped:
        return CheckResult(
            halted=True,
            reason=f"unmapped types: {len(diff.unmapped)} member(s) could not be mapped to a tier rule",
        )

    # Collect card_ids from diff entries that will write to UniFi.
    write_card_ids: list[int] = []
    for r in diff.to_add:
        if r.card_id is not None:
            write_card_ids.append(r.card_id)
    for r, _ in diff.to_update_credential:
        if r.card_id is not None:
            write_card_ids.append(r.card_id)

    # Guard 2: duplicate card IDs
    seen: set[int] = set()
    for cid in write_card_ids:
        if cid in seen:
            return CheckResult(
                halted=True,
                reason=f"duplicate card ID {cid} appears in multiple diff entries",
            )
        seen.add(cid)

    # Guard 3: invalid card ID
    for cid in write_card_ids:
        if cid < 0 or cid > 65535:
            return CheckResult(
                halted=True,
                reason=f"invalid card ID {cid}: must be in range 0..65535",
            )

    # Guards 4–6: mass guards, skipped when baseline is too small for percentages to be meaningful.
    if baseline >= thresholds.baseline_floor:
        # Guard 4: mass deactivation
        if len(diff.to_deactivate) / baseline > thresholds.mass_deactivate_pct:
            pct = len(diff.to_deactivate) / baseline * 100
            return CheckResult(
                halted=True,
                reason=(
                    f"mass deactivation: {len(diff.to_deactivate)} of {baseline} active users "
                    f"({pct:.1f}%) exceeds {thresholds.mass_deactivate_pct * 100:.1f}% threshold"
                ),
            )

        # Guard 5: mass addition
        if len(diff.to_add) / baseline > thresholds.mass_add_pct:
            pct = len(diff.to_add) / baseline * 100
            return CheckResult(
                halted=True,
                reason=(
                    f"mass addition: {len(diff.to_add)} new users vs baseline {baseline} "
                    f"({pct:.1f}%) exceeds {thresholds.mass_add_pct * 100:.1f}% threshold"
                ),
            )

        # Guard 6: mass policy change
        if len(diff.to_update_policy) / baseline > thresholds.mass_policy_pct:
            pct = len(diff.to_update_policy) / baseline * 100
            return CheckResult(
                halted=True,
                reason=(
                    f"mass policy change: {len(diff.to_update_policy)} of {baseline} active users "
                    f"({pct:.1f}%) exceeds {thresholds.mass_policy_pct * 100:.1f}% threshold"
                ),
            )

    return CheckResult(halted=False, reason=None)
