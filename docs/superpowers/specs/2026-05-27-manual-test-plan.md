# Manual Test Plan — Local Validation Before Pi Deployment

**Date:** 2026-05-27
**Goal:** Validate the full door-sync reconciliation pipeline from a local machine against production CiviCRM (read-only) and a test UniFi Access controller (read+write), building confidence before deploying to the Raspberry Pi.

**Environment:**
- Local machine (macOS)
- Production CiviCRM — read-only (no writes to CiviCRM ever)
- Test UniFi Access controller — full read+write access
- Config and secrets set up from scratch

---

## Phase 1: Config Setup & Validation

**Goal:** Get `config.toml` and `.env` working locally with real credentials. Confirm the app loads without errors.

### Steps

1. Copy `config.example.toml` to `config.toml` and fill in:
   - `civicrm.host` — production CiviCRM base URL
   - `unifi.host` — test controller URL (port 12445)
   - `unifi.tls_fingerprint` — SHA-256 of the test controller's TLS cert
   - `unifi.facility_code` — Wiegand-26 facility code for your site
   - `tier_mapping.rules` — at least 2 membership types with different policies
   - `safety` thresholds — keep defaults
   - `ops` paths — point at `/tmp/door-sync/` for local testing
2. Create `.env` with `CIVICRM_API_KEY` and `UNIFI_API_KEY`.
3. Run:
   ```bash
   uv run door-sync validate-config
   ```
   Expect exit code 0 and no issues printed.
4. Intentionally break one value (e.g., `cadence_seconds = 5`) and re-run — confirm it catches the error with a clear message.

### Pass Criteria

- `validate-config` exits 0 with your real config.
- Intentional errors are caught with clear, actionable messages.

---

## Phase 2: Read-Only Validation

**Goal:** Confirm both API clients can authenticate and fetch data correctly. No writes happen in this phase.

### Steps

1. Run:
   ```bash
   uv run door-sync show-diff
   ```
   This fetches from CiviCRM (prod) and UniFi (test controller), computes the diff, and prints it.

2. **Verify CiviCRM fetch:**
   - Member names and contact IDs appear reasonable (cross-reference a few against the CiviCRM admin UI).
   - Card IDs are redacted in log output (last-4 only) — check stderr with `-v`.
   - Members with no card ID or no active membership should be excluded.

3. **Verify UniFi fetch:**
   - Existing UniFi users appear (if the test controller has pre-populated users).
   - If the test controller is empty, the diff should show all CiviCRM members as `to_add`.

4. **Verify diff output:**
   - Do the `to_add` / `to_deactivate` / `to_update_*` / `unmapped` buckets make sense given what you know about the two systems?
   - Any members in `unmapped`? Confirm their membership types aren't in your tier rules.

5. **Check error handling:**
   - Temporarily use a wrong API key in `.env` and re-run.
   - Confirm it fails with a clear auth error, not an unhandled traceback.

### Pass Criteria

- `show-diff` exits 0.
- Printed diff matches expectations.
- Card IDs are redacted in verbose logs.
- Bad credentials produce clean error messages.

---

## Phase 3: Dry-Run Cycle

**Goal:** Run a full reconcile cycle with `--dry-run` to validate the orchestrator, safety guards, audit logging, and state tracking — without writing to UniFi.

### Steps

1. Create ops directories:
   ```bash
   mkdir -p /tmp/door-sync
   ```

2. Run:
   ```bash
   uv run door-sync run --once --dry-run -v
   ```

3. **Verify orchestrator flow** (stderr with `-v`):
   - Config loaded → CiviCRM fetch → tier resolution → UniFi fetch → diff computed → safety check → (dry-run) apply skipped → cycle complete.
   - Exit code 0 (success) or 1 (halted by safety guard).

4. **Check audit log** — read `/tmp/door-sync/audit.jsonl`:
   - One JSONL entry for this cycle.
   - Entry includes the diff summary (counts per bucket).
   - Card IDs are redacted (last-4 only).
   - Entry is marked as `dry_run: true`.

5. **Check state file** — read `/tmp/door-sync/state.json`:
   - `last_success` has an ISO timestamp (if not halted).
   - `run_count` is 1.

6. **Test safety guard trigger:**
   - Temporarily empty or misconfigure tier rules so many members become `unmapped`.
   - Re-run with `--dry-run`.
   - Expect exit code 1 (halted).
   - Audit log should show a halt entry with the reason.
   - Alert flag file should be created at configured path.
   - State file should show `last_halt` with reason.

7. **Verify alert flag lifecycle:**
   - Check that the flag file exists and contains the halt reason.
   - Restore correct tier rules, re-run dry-run successfully.
   - Confirm the flag file is cleared.

### Pass Criteria

- Dry-run produces correct audit entries marked `dry_run: true`.
- State tracking increments correctly.
- Safety guards fire on misconfigured tier rules.
- Alert flag is created on halt and cleared on success.

---

## Phase 4: Live Writes to Test Controller

**Goal:** Validate the full write path — user creation, NFC card binding, policy assignment, credential updates, and deactivation on the real UniFi Access test controller.

**Pre-requisites:** Phases 1–3 passing. You understand the diff from Phase 2/3.

### 4a. Small-Batch Initial Sync

1. Restrict scope: configure tier rules to match only one membership type with few members.
2. Run:
   ```bash
   uv run door-sync run --once -v
   ```
3. Verify in the UniFi Access admin UI:
   - New users appeared with correct `display_name` and `employee_number` (contact_id).
   - NFC card credentials are bound with the correct card ID and facility code.
   - Users are assigned to the correct access policy.
   - Users are marked active.

### 4b. Idempotency Check

1. Immediately re-run:
   ```bash
   uv run door-sync run --once -v
   ```
2. Diff should be empty — no adds, updates, or deactivations.
3. Audit log should show a new entry with zero changes.
4. Verify no duplicate users or credentials in UniFi admin UI.

### 4c. Credential Update

1. In CiviCRM, find or arrange a test member whose card ID differs between systems.
2. Run `uv run door-sync show-diff` — confirm the member appears in `to_update_credential`.
3. Run `uv run door-sync run --once -v` to apply.
4. Verify in UniFi admin: old credential removed, new one bound with correct facility code.

### 4d. Policy Update

1. Change a test member's membership type in CiviCRM so their tier mapping resolves to a different policy.
2. Run `show-diff` — confirm `to_update_policy`.
3. Run `--once` — verify in UniFi admin: user's policy changed.

### 4e. Deactivation

1. In CiviCRM, expire or remove a test member's active membership (or clear their card ID).
2. Run `show-diff` — confirm `to_deactivate`.
3. Run `--once` — verify in UniFi admin: the user's NFC card is **removed** (freed for reuse) and the user is deactivated (not deleted, just inactive). The card delete is issued *before* the status change.

### 4f. Card Reuse and Reclaim

Validates that a recycled physical card can move to a new member — the path that
previously crashed every cycle with `CODE_CREDS_NFC_HAS_BIND_USER`.

**Recycled card, holder freed on deactivation:**

1. Take member A holding card X in UniFi. Deactivate A (4e) and confirm card X was removed.
2. In CiviCRM, assign card X to a different member B. Run `show-diff` (B in `to_add` or `to_update_credential`), then `--once`.
3. Verify in UniFi admin: card X is bound to B, and B is active.

**Reclaim from an already-disabled holder** (the production case — a card still
stuck on a user that was deactivated *before* this behavior shipped):

4. Arrange member C **deactivated** in UniFi but still holding card Y (set this up in the UniFi admin UI if needed).
5. In CiviCRM, assign card Y to member D. Run `--once`.
6. Verify the operational log shows a single `WARNING ... reclaiming it for this member` line — redacted card (`****NNNN`) and `contact=<id>`, **no member name** — and that UniFi admin now shows card Y bound to D. No alert/crash for D.

**Active-holder guard** (door-sync must NOT displace an active user):

7. Arrange card Z bound to an *active* UniFi account (e.g. a manually-enrolled admin). Assign card Z to a member in CiviCRM and run `--once`.
8. Verify the cycle records a per-user failure that identifies the holder by id (not name) and leaves card Z on the active account untouched.

### 4g. Full Sync

1. Remove the tier rule restriction from step 4a — allow all membership types.
2. Run `show-diff` to review the full diff.
3. If the diff looks correct, run `--once` to apply.
4. Immediately re-run `--once` — confirm idempotent (empty diff).

### Pass Criteria

- All five write operations work correctly: add user, bind NFC card, assign policy, update credential, deactivate user.
- Deactivation frees the member's NFC card (the number becomes reusable).
- A recycled card moves to its new member; a card stuck on a disabled holder is reclaimed (single redacted warning, no alert); a card on an *active* holder is left untouched with an actionable per-user error.
- Idempotency holds after each operation (re-run produces empty diff).
- Audit log and state file reflect each cycle accurately.
- No duplicate users or credentials created.

---

## Summary

| Phase | What it validates | Risk if skipped |
|-------|------------------|-----------------|
| 1. Config | Config loading, validation, secrets | Cryptic API errors from bad config |
| 2. Read-only | API auth, data fetch, diff accuracy | Wrong diff → wrong writes |
| 3. Dry-run | Orchestrator flow, safety, audit, state | Safety guards untested before live writes |
| 4. Live writes | Full write path on test controller | Deploy to Pi without knowing writes work |

Each phase gates the next. Do not proceed to live writes until dry-run is clean.
