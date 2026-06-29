"""Single reconcile entry point. Wires CiviCRM + UniFi clients + pure
modules + audit + alert + state per architecture §10.

Invariants:
  - No globals; everything comes from `config`.
  - Clients are constructed per cycle (cheap; gives clean isolation).
  - Pure modules behave identically in dry-run and live.
  - Exceptions propagate — this function does not catch. __main__ does.
"""

import logging

from door_sync import alert, audit, reconciler, safety, state, tier_mapping
from door_sync.civicrm.client import CivicrmClient
from door_sync.config import AlertConfig, Config, OpsPaths
from door_sync.models import ReconcileResult
from door_sync.unifi.client import UnifiClient

_logger = logging.getLogger("door_sync.orchestrator")


def handle_crash(
    exc: Exception,
    *,
    paths: OpsPaths,
    alert_config: AlertConfig | None = None,
) -> None:
    """Log, audit, and alert on a reconcile cycle crash.

    Shared by one-shot (--once) and daemon mode so behavior stays symmetric.

    Args:
        exc: The exception that caused the crash.
        paths: Operational file paths (audit log, state, alert flag).
        alert_config: Email transport settings, or None for flag-file only.
    """
    _logger.error("reconcile crashed", exc_info=exc)
    audit.log_crashed(exc, path=paths.audit_jsonl)
    exc_msg = str(exc)
    if len(exc_msg) > 200:
        exc_msg = exc_msg[:200] + "..."
    alert.raise_(
        f"crashed: {type(exc).__name__}: {exc_msg}",
        path=paths.alert_flag,
        alert_config=alert_config,
    )


def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:
    """Execute one reconciliation cycle: fetch, diff, check, apply.

    Args:
        config: Full application configuration.
        dry_run: If True, compute and audit the diff but skip UniFi writes.

    Returns:
        `ReconcileResult` indicating whether the cycle was halted or applied.
    """
    paths = config.ops_paths

    with (
        CivicrmClient(config.civicrm) as civicrm,
        UnifiClient(
            config.unifi,
            dry_run=dry_run,
            managed_policy_ids=tier_mapping.managed_policy_ids(config.tier_mapping),
        ) as unifi,
    ):
        civi_members = civicrm.fetch_active()
        resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
        for r in resolved:
            if r.resolution == "tier" and r.email is None:
                _logger.warning(
                    "contact %d resolves to a door tier but has no email in CiviCRM; "
                    "provisioning without invite delivery",
                    r.contact_id,
                )
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
            alert.raise_(
                check.reason or "halted",
                path=paths.alert_flag,
                alert_config=config.alert,
            )
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
            alert.clear(path=paths.alert_flag, alert_config=config.alert)
        return ReconcileResult(halted=False, reason=None, diff=diff)
