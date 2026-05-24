"""Single reconcile entry point. Wires CiviCRM + UniFi clients + pure
modules + audit + alert + state per architecture §10.

Invariants:
  - No globals; everything comes from `config`.
  - Clients are constructed per cycle (cheap; gives clean isolation).
  - Pure modules behave identically in dry-run and live.
  - Exceptions propagate — this function does not catch. __main__ does.
"""

from door_sync import alert, audit, reconciler, safety, state, tier_mapping
from door_sync.civicrm.client import CivicrmClient
from door_sync.config import Config
from door_sync.models import ReconcileResult
from door_sync.unifi.client import UnifiClient


def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:
    paths = config.ops_paths

    with (
        CivicrmClient(config.civicrm) as civicrm,
        UnifiClient(config.unifi, dry_run=dry_run) as unifi,
    ):
        civi_members = civicrm.fetch_active()
        resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
        unifi_users = unifi.fetch_users()

        diff = reconciler.compute_diff(resolved, unifi_users)
        active_baseline = sum(1 for u in unifi_users if u.active)
        check = safety.check(diff, baseline=active_baseline, thresholds=config.safety)

        if check.halted:
            audit.log_halt(
                check.reason or "",
                diff,
                dry_run=dry_run,
                path=paths.audit_jsonl,
                facility_code=config.unifi.facility_code,
            )
            # Raise alert even in dry-run: an operator running --dry-run still
            # wants to know if safety would halt. The flag is cleared only by a
            # successful live cycle.
            alert.raise_(check.reason or "halted", path=paths.alert_flag)
            if not dry_run:
                state.write_halt(paths.state_json, check.reason or "")
            return ReconcileResult(halted=True, reason=check.reason, diff=diff)

        unifi.apply(diff)
        audit.log_applied(
            diff,
            dry_run=dry_run,
            path=paths.audit_jsonl,
            facility_code=config.unifi.facility_code,
        )
        if not dry_run:
            state.write_success(paths.state_json)
            alert.clear(path=paths.alert_flag)
        return ReconcileResult(halted=False, reason=None, diff=diff)
