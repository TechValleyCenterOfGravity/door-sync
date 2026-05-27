"""CLI pretty-printers used by __main__'s show-diff and validate-config.

Kept separate from __main__.py so they're unit-testable without subprocess.
"""

from typing import IO

from door_sync.config import ConfigIssue
from door_sync.models import Diff, ResolvedMember, TierMapping, UnifiUser


def print_membership_types(
    seen_types: set[str], tier_mapping: TierMapping, *, file: IO[str]
) -> None:
    """Print all membership types with their mapping status.

    Args:
        seen_types: Membership type labels found in CiviCRM.
        tier_mapping: Tier mapping configuration with rules.
        file: Output stream to write to.
    """
    mapped = seen_types & tier_mapping.rules.keys()
    unmapped = seen_types - tier_mapping.rules.keys()
    unused = tier_mapping.rules.keys() - seen_types

    print(f"=== MEMBERSHIP TYPES ({len(seen_types)} seen) ===", file=file)
    for t in sorted(mapped):
        rule = tier_mapping.rules[t]
        print(f"  [mapped]   {t} -> {rule.resolution} (rank {rule.rank})", file=file)
    for t in sorted(unmapped):
        print(f"  [UNMAPPED] {t}", file=file)
    for t in sorted(unused):
        rule = tier_mapping.rules[t]
        print(
            f"  [unused]   {t} -> {rule.resolution} (rank {rule.rank}, no members)",
            file=file,
        )
    print(file=file)


def print_diff(diff: Diff, *, file: IO[str]) -> None:
    """Print a human-readable summary of a computed diff.

    Args:
        diff: The reconciliation diff to display.
        file: Output stream to write to.
    """
    print(f"=== ADD ({len(diff.to_add)}) ===", file=file)
    for m in diff.to_add:
        print(_format_member(m), file=file)

    print(f"=== UPDATE CREDENTIAL ({len(diff.to_update_credential)}) ===", file=file)
    for m, _u in diff.to_update_credential:
        print(_format_member(m), file=file)

    print(f"=== UPDATE POLICY ({len(diff.to_update_policy)}) ===", file=file)
    for m, _u in diff.to_update_policy:
        print(_format_member(m), file=file)

    print(f"=== DEACTIVATE ({len(diff.to_deactivate)}) ===", file=file)
    for u in diff.to_deactivate:
        print(_format_user(u), file=file)

    print(f"=== UNMAPPED ({len(diff.unmapped)}) ===", file=file)
    for m in diff.unmapped:
        print(_format_member(m), file=file)


def print_config_issues(issues: list[ConfigIssue], *, file: IO[str]) -> None:
    """Print configuration validation issues, one per line.

    Args:
        issues: List of config validation errors.
        file: Output stream to write to.
    """
    for issue in issues:
        print(f"{issue.path}: {issue.message}", file=file)


def _format_member(m: ResolvedMember) -> str:
    parts = [str(m.contact_id), m.display_name]
    if m.card_id is not None:
        parts.append(f"[card_last4={_last4(m.card_id)}]")
    if m.target_policy is not None:
        parts.append(f"[policy={m.target_policy}]")
    return " ".join(parts)


def _format_user(u: UnifiUser) -> str:
    parts = [str(u.contact_id), u.display_name]
    if u.card_id is not None:
        parts.append(f"[card_last4={_last4(u.card_id)}]")
    if u.policy is not None:
        parts.append(f"[policy={u.policy}]")
    return " ".join(parts)


def _last4(card_id: int) -> str:
    return format(card_id, "X")[-4:]
