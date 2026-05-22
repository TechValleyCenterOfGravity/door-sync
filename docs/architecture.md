# Sync Service — Architecture Reference

**Version:** 1.0 (Draft)
**Status:** Architecture decided; details pending
**Audience:** Coding agents and human developers implementing or extending the door-access sync service
**Companion document:** `access-control-design-guide-v1-draft.md` (product requirements, system context, runbook)
**Last updated:** May 2026

---

## 1. Scope

This document specifies the *internal architecture* of the door-access sync service: process model, module layout, data contracts, pure/impure boundaries, and coding conventions.

It does **not** restate:
- Product requirements or business logic (see design guide §2, §6, §7, §8)
- The operational runbook (see design guide §11)
- Hardware, networking, or security configuration (see design guide §4, §5, §12)

Agents implementing or modifying this service should treat this document as authoritative for *how the code is structured* and the design guide as authoritative for *what the code must do*. When they appear to disagree, the design guide wins on requirements and this document wins on shape.

---

## 2. Quick orientation

The sync service is a Raspberry Pi process that reconciles CiviCRM members against UniFi Access users on a polling cycle. Each cycle: pull the active set from CiviCRM, pull the current set from UniFi, compute a four-way diff, run safety guards, apply the diff via the UniFi Access API.

**The single most important architectural property:** the sync service is **not** in the critical path for door authorization. Doors authorize locally against credentials cached on the Retrofit Hub. The sync is an eventually-consistent reconciler. This means:

- Latency budget for sync changes is minutes, not milliseconds
- Sync downtime is operationally inconvenient but not security-affecting
- Idempotency and dry-run are mandatory; concurrency and low-latency are not

Treat any pressure to make the service "faster" or "real-time" as suspicious. The polling cadence is intentional.

---

## 3. Process model

**Long-running systemd service.** Not a timer-triggered script, not a cron job. A single Python process started by systemd that runs until SIGTERM.

- **Internal scheduling:** a loop that calls `orchestrator.reconcile()`, then waits `config.cadence_seconds` (default 600), then runs again. The wait uses `threading.Event.wait(timeout=...)` so SIGTERM can interrupt cleanly.
- **Shutdown:** SIGTERM sets the event; the loop checks the flag after each cycle and exits if set. In-flight reconciliations finish; no work is interrupted mid-cycle.
- **Per-cycle errors:** each cycle is wrapped in `try/except Exception` in `scheduler.py`. Exceptions are logged at ERROR and the cycle is skipped (design guide §8 fail-safe). The loop continues to the next cycle.

**Sync, not async.** No asyncio anywhere. This is deliberate:

- The service has one busy moment per polling cycle. Concurrency benefit is nil.
- The successor-IT-volunteer constraint (design guide §2 non-functional) prioritizes accessible code over modern idioms.
- HTTP via `httpx` in sync mode. When the Appendix C webhook receiver lands, it lands as Flask + waitress in a second thread of the same process — not as FastAPI + asyncio.

**Do not refactor to async** without explicit direction from a human maintainer.

---

## 4. Module layout

```
door-sync/
├── pyproject.toml
├── README.md
├── src/door_sync/
│   ├── __init__.py
│   ├── __main__.py          # entry point: python -m door_sync
│   ├── config.py            # load env + TOML, validate, freeze into Config
│   ├── models.py            # all domain dataclasses
│   ├── tier_mapping.py      # resolve(member, mapping) -> ResolvedMember
│   ├── civicrm/
│   │   ├── __init__.py
│   │   └── client.py        # read-only API4 client
│   ├── unifi/
│   │   ├── __init__.py
│   │   └── client.py        # read+write client; honors dry_run flag
│   ├── reconciler.py        # PURE: compute_diff(resolved, unifi) -> Diff
│   ├── safety.py            # PURE: check(diff, baseline, thresholds) -> CheckResult
│   ├── orchestrator.py      # the reconcile() function
│   ├── scheduler.py         # the loop; signal handling
│   ├── audit.py             # JSON-lines structured audit log
│   ├── state.py             # last-success timestamp; atomic-rename writes
│   ├── alert.py             # halt-and-alert dispatch
│   └── webhook.py           # (future, Appendix C) Flask app; calls orchestrator
├── tests/
│   ├── test_reconciler.py
│   ├── test_safety.py
│   ├── test_tier_mapping.py
│   ├── test_orchestrator.py
│   └── fixtures/
└── systemd/
    ├── door-sync.service
    └── door-sync.env.example
```

**Responsibility per module:**

| Module | Owns | Depends on |
|---|---|---|
| `config` | Loading and validating env + TOML into a frozen `Config` | stdlib only |
| `models` | All domain dataclasses (`CiviMember`, `ResolvedMember`, `UnifiUser`, `Diff`, …) | stdlib only |
| `tier_mapping` | Resolving a `CiviMember` into a `ResolvedMember` given the mapping | `models` |
| `civicrm.client` | Reading active members from CiviCRM API4 | `models`, `httpx` |
| `unifi.client` | Reading users from UniFi Access; applying a `Diff`; dry-run flag | `models`, `httpx` |
| `reconciler` | Computing a `Diff` (PURE) | `models` |
| `safety` | Checking a `Diff` against thresholds and integrity rules (PURE) | `models`, `config` (for thresholds) |
| `orchestrator` | One function: `reconcile(config, *, dry_run) -> ReconcileResult` | everything above |
| `scheduler` | The main loop; signal handling | `orchestrator`, `config` |
| `audit` | JSON-lines structured log of every diff applied or halted | `models` |
| `state` | Persisting last-success timestamp | stdlib only |
| `alert` | Sending alerts on halt or repeated failure | `config` |
| `webhook` | (future) HTTP receiver for Appendix C day pass flow | `orchestrator`, `unifi.client` |

**Strict layering:** modules higher in this table do not import modules lower. The orchestrator imports everything; nothing imports the orchestrator except `scheduler` and (eventually) `webhook`.

---

## 5. The pure/impure boundary

`reconciler`, `safety`, and `tier_mapping` are **pure**:

- Take dataclasses in, return dataclasses out
- No I/O — no HTTP, no file reads, no logging side effects
- No global state; do not mutate arguments
- Deterministic given inputs

**Why this matters:**

- Unit tests for these modules use plain dataclass construction. No mocks, no fixtures, no HTTP doubles. Test runs are millisecond-fast.
- The dry-run mechanism is trustworthy: dry-run is a flag inside `UnifiClient` that turns writes into logged no-ops. The pure modules execute identically in dry-run and live, so a clean dry-run on production data implies the next live run will compute and attempt the same diff.
- These three modules carry the entire correctness story of the service. The clients are thin and obvious; the orchestrator is wiring.

**Do not:**

- Add logging calls inside `reconciler.py`, `safety.py`, or `tier_mapping.py`. The orchestrator logs the inputs and outputs of these calls.
- Add config lookups inside the pure modules. Pass the relevant slice in as an argument.
- Make pure modules raise exceptions on data integrity issues. Return a sentinel value (e.g., `ResolvedMember.resolution = "unmapped"` or `CheckResult(halted=True, ...)`) and let the orchestrator decide.

---

## 6. Data contracts

These dataclasses are the contract between modules. All are `frozen=True` to prevent accidental mutation; diffs in particular must be immutable snapshots once computed.

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class CiviMember:
    contact_id: int
    display_name: str
    card_id: int | None
    membership_types: list[str]     # all active types for this contact

@dataclass(frozen=True)
class ResolvedMember:
    contact_id: int
    display_name: str
    card_id: int | None
    target_policy: str | None       # None when resolution != "tier"
    resolution: Literal["tier", "none", "day-pass", "unmapped"]

@dataclass(frozen=True)
class UnifiUser:
    contact_id: int                 # stored in employee_number field
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
    unmapped: list[ResolvedMember]  # populated only when tier mapping has gaps

@dataclass(frozen=True)
class CheckResult:
    halted: bool
    reason: str | None              # human-readable, populated when halted=True

@dataclass(frozen=True)
class ReconcileResult:
    halted: bool
    reason: str | None
    diff: Diff | None               # the diff that was applied (or would have been)
```

**Reconciliation key:** `contact_id`, persisted in UniFi Access's `employee_number` field. Card ID is not a stable key (cards get reissued).

**Naming note:** `to_update_credential` covers both card_id changes *and* display_name changes, matching the design guide §7. The display_name isn't strictly a "credential" but lives on the same UniFi user record; keeping it in one diff set avoids splitting writes that target the same API endpoint.

**When extending the model:**

- New fields go on existing dataclasses where they belong semantically. Don't add parallel dicts.
- New domain concepts get their own frozen dataclass in `models.py`.
- Don't subclass dataclasses; prefer composition or a new dataclass.

---

## 7. The reconcile cycle

```
config.load()
      │
      ├─→ civicrm.fetch_active() ──→ List[CiviMember]
      │                                     │
      │                                     ↓
      │                        tier_mapping.resolve_all()
      │                                     │
      │                                     ↓
      │                              List[ResolvedMember] ─┐
      │                                                    │
      └─→ unifi.fetch_users()  ──→ List[UnifiUser] ────────┤
                                                           ↓
                                              reconciler.compute_diff()
                                                           │
                                                           ↓
                                                         Diff
                                                           │
                                                           ↓
                                                   safety.check()
                                                      ↓        ↓
                                                    Halt       Ok
                                                      │        │
                                                  audit.log  unifi.apply(diff)
                                                  alert.send    │
                                                              audit.log
                                                                │
                                                          state.write_last_success()
```

**Step-by-step semantics:**

1. **Pull from CiviCRM** — filter to contacts with non-empty `card_id` AND membership status ∈ {Current, Grace}. Include each contact's active Membership Type(s). Single API call, paginated if needed.
2. **Resolve each member** — `tier_mapping.resolve(member, mapping)` produces a `ResolvedMember`. Resolution branches:
   - `tier` → `target_policy` populated; member should have credential + policy in UniFi
   - `none` → member intentionally does not get door access; deactivate any existing UniFi record
   - `day-pass` → skip entirely; do not provision, do not deactivate, leave any existing record alone. Day pass flow (Appendix C) handles these on a per-visit basis.
   - `unmapped` → halt signal; recorded in `Diff.unmapped`
   - Highest-wins rule when a contact has multiple active memberships at different tiers (design guide §6).
3. **Pull from UniFi** — all users with `employee_number` populated (sync-managed). Users without `employee_number` are admin-created and ignored by the reconciler.
4. **Compute diff** — see §8.
5. **Safety check** — see §9.
6. **Apply diff** — `unifi.apply(diff)` iterates the diff sets serially with a 50-100ms inter-call delay. Each action retried per design guide §8 (exponential backoff, honor `Retry-After` on 429). Failures partway through are tolerable: idempotency means the next cycle resumes correctly.
7. **Log + persist state** — write audit entries; write last-success timestamp atomically (write to temp, fsync, rename).

---

## 8. The diff algorithm

```python
reconciler.compute_diff(
    resolved: list[ResolvedMember],
    unifi: list[UnifiUser],
) -> Diff
```

Index both lists by `contact_id`. For each `contact_id` present in either:

| Resolved (CiviCRM side) | UniFi side | Action |
|---|---|---|
| `tier` resolution | not present | **to_add** |
| `tier` resolution | present, active, card_id or display_name differs | **to_update_credential** |
| `tier` resolution | present, active, policy differs from target | **to_update_policy** |
| `tier` resolution | present, active, no differences | (no-op) |
| `tier` resolution | present, inactive | **to_add** (reactivate path) |
| `none` resolution | present, active | **to_deactivate** |
| `none` resolution | present, inactive *or* not present | (no-op) |
| `day-pass` resolution | any | (no-op; explicitly skip — do not touch existing records) |
| `unmapped` resolution | n/a | append to **unmapped** list; do not include in other diff sets |
| not present in resolved | present, active | **to_deactivate** |
| not present in resolved | present, inactive *or* not present | (no-op) |

A contact may appear in both `to_update_credential` and `to_update_policy` in the same diff if both changed. Both updates are applied.

**Idempotency canary:** running `compute_diff` immediately after a successful `unifi.apply()` must produce a `Diff` with all empty sets. This is the canonical test for the algorithm — include it in the test suite.

---

## 9. Safety guards

```python
safety.check(
    diff: Diff,
    *,
    baseline: int,
    thresholds: SafetyThresholds,
) -> CheckResult
```

Returns `CheckResult(halted=True, reason=...)` if any guard fires. Any single trigger halts the entire cycle; no partial application.

| Guard | Trigger | Default threshold |
|---|---|---|
| Mass deactivation | `len(diff.to_deactivate) / baseline > X` | 15% |
| Mass addition | `len(diff.to_add) / baseline > X` | 25% |
| Mass policy change | `len(diff.to_update_policy) / baseline > X` | 20% |
| Unmapped types | `len(diff.unmapped) > 0` | any |
| Duplicate card IDs | any two `ResolvedMember`s share a non-None `card_id` | any |
| Invalid card ID | any `card_id` outside 0–65535 | any |

`baseline` is the count of *active* UniFi users at the start of the cycle. When baseline is below a configurable floor (default 10), percentage guards are skipped — they're meaningless on tiny populations and would block legitimate initial provisioning.

Thresholds live in `Config.safety` and are configurable per deployment.

**Fail-secure default:** any guard firing means zero writes occur this cycle. The alert is dispatched, the audit log records the halt, and the next cycle re-runs from scratch.

---

## 10. The orchestrator

The single entry point to a reconciliation cycle:

```python
def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:
    civicrm = CivicrmClient(config.civicrm)
    unifi = UnifiClient(config.unifi, dry_run=dry_run)

    civi_members = civicrm.fetch_active()
    resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
    unifi_users = unifi.fetch_users()

    diff = reconciler.compute_diff(resolved, unifi_users)
    active_baseline = sum(1 for u in unifi_users if u.active)
    check = safety.check(diff, baseline=active_baseline, thresholds=config.safety)

    if check.halted:
        audit.log_halt(check.reason, diff)
        alert.send(check.reason)
        return ReconcileResult(halted=True, reason=check.reason, diff=diff)

    unifi.apply(diff)
    audit.log_applied(diff)
    state.write_last_success()
    return ReconcileResult(halted=False, reason=None, diff=diff)
```

**Invariants:**

- No globals. Everything comes from `config`.
- Clients are constructed per cycle. They're cheap to instantiate; this gives clean per-cycle isolation and avoids stale HTTP session state.
- The orchestrator owns I/O ordering; pure modules own correctness.
- Exceptions propagate to the scheduler. The orchestrator does not catch.
- The same function is the entry point for: the scheduler's loop, a `python -m door_sync run --once` CLI command, the future webhook handler's "trigger immediate sync" endpoint.

---

## 11. Conventions

**Typing.** Type hints everywhere, including private functions. Run `mypy --strict` or pyright in strict mode in CI.

**Dataclasses.** All domain objects are `@dataclass(frozen=True)`. Mutable containers (`list`, `dict`) inside a frozen dataclass are allowed but treated as conceptually immutable — never mutate them in place; construct a new dataclass with the updated value.

**Dependency injection.** Pass dependencies as arguments. No module-level singletons for clients, config, or loggers (other than the stdlib logging tree).

**HTTP.** `httpx` in sync mode. One `httpx.Client` per client class instance. TLS verification on; the UniFi self-signed cert is handled by pinning fingerprint via config (design guide §12), not by disabling verification.

**Logging.** Two streams:

- **Operational logging** via stdlib `logging` to systemd journal (stderr inherited). Levels: DEBUG, INFO, WARN, ERROR. INFO for normal cycle output; WARN for retryable failures; ERROR for halts and crashes.
- **Audit logging** via `audit.py` to a dedicated JSON-lines file under `/var/log/door-sync/audit.jsonl`, rotated by logrotate. Every diff applied or halted produces one or more audit records. This stream is for human and tool consumption (incident review, reporting); it is not for debugging.

**Card ID redaction.** Card IDs appear in audit logs as last-4-digits only. Never log full card IDs at any level.

**Tests.** `pytest`. Layout mirrors the source tree. Pure-module tests have zero HTTP. Client tests use recorded fixtures (`vcrpy` or hand-written fakes — pick one and stay consistent across the codebase). The orchestrator has at least one integration test with both clients faked.

**Imports.**
- Standard library first
- Third-party second
- `door_sync.*` third
- No `from x import *`

**Errors.**
- Pure modules: return sentinel values (e.g., `resolution="unmapped"`, `CheckResult(halted=True)`) — do not raise.
- Clients: raise on HTTP errors after exhausting retries. The orchestrator does not catch; the scheduler catches and continues.
- Never use bare `except:` or `except Exception:` without re-raising or explicitly logging at ERROR.

---

## 12. What this document does not yet specify

These decisions are intentionally deferred. When implementing them, update this document and remove the item from this list.

| Topic | Where it'll land | Notes |
|---|---|---|
| Config schema (TOML structure, env vars, validation rules) | `config.py` + new §13 here | Decide before writing any client |
| CiviCRM client API surface (method signatures) | `civicrm/client.py` + new §14 here | Determined by API4 query needs |
| UniFi client API surface (method signatures) | `unifi/client.py` + new §15 here | Constrained by UniFi Access API capabilities |
| Audit log entry schema | `audit.py` + new §16 here | JSON line per record; fields TBD |
| Alerting transport | `alert.py` + new §17 here | Likely SMTP or webhook to existing space ops channel |
| Retry/backoff specifics | inside both clients | Per design guide §8 |
| Packaging and deployment specifics | `pyproject.toml` + README | Standard pip-into-venv expected |

---

## 13. Future evolution: Appendix C webhook receiver

When the day pass flow (design guide Appendix C) is implemented, the architecture extends as follows:

- **New module:** `webhook.py` containing a Flask application
- **Process layout:** Flask runs via `waitress` in a second thread of the same daemon; main thread continues to run the scheduler loop
- **New endpoints:** `POST /day-pass/provision` and `POST /day-pass/revoke`, both authenticated by HMAC against a shared secret in env
- **Idempotency:** every webhook handler checks for an existing UniFi visitor schedule for the given email before creating one; duplicate deliveries are no-ops
- **Shared code:** webhook handlers call into a new `unifi.client.visitor_*` method family; they do not call `orchestrator.reconcile()` (the day pass flow operates on visitors, not member users)
- **API key separation:** webhook handlers use a separate UniFi API key with Visitor scope only (design guide §5)

**What must not change** when this evolution lands:

- The reconciler, safety, and tier_mapping modules remain pure and untouched
- The orchestrator's `reconcile()` signature does not change
- The scheduler continues to run on its existing cadence; the webhook does not "speed up" the scheduler
- No async/await migration. The webhook receiver is sync Flask.

If an agent finds itself wanting to refactor the scheduler or orchestrator to support webhooks, that's a sign the design has drifted. The webhook should *use* the existing pieces, not reshape them.

---

## 14. Cross-references to the design guide

| Design guide section | What it covers |
|---|---|
| §2 | Functional and non-functional requirements |
| §5 | Sync service runtime expectations (`/etc/door-sync/env`, systemd packaging) |
| §6 | Data model and tier mapping rules |
| §7 | Sync algorithm |
| §8 | Safety guards — source of truth for guard semantics |
| §10 | Failure modes that constrain this architecture |
| §11 | Operational runbook — the operator-facing side |
| §12 | Security considerations (API key handling, TLS verification) |
| Appendix C | Day pass flow — the future evolution this architecture accommodates |