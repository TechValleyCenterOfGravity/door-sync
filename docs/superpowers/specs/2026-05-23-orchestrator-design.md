# Orchestrator + ops stubs — design

**Date:** 2026-05-23
**Status:** Approved for planning
**Companion:** [`docs/architecture.md`](../../architecture.md) §4 (module table — `orchestrator`, `audit`, `alert`, `state`), §7 (the reconcile cycle), §10 (orchestrator invariants), §11 (logging, redaction, error conventions). This spec also closes architecture §12's "Audit log entry schema" and partially closes "Alerting transport" (flag-file stub now; transport TBD).

---

## 1. Goal

Wire the merged CiviCRM client, UniFi client, and pure modules (`reconciler`, `safety`, `tier_mapping`) into a single `reconcile(config, *, dry_run) -> ReconcileResult` function, plus the smallest possible `audit`, `alert`, and `state` modules that satisfy the architecture's operational contract. Expose the result through a `door-sync` CLI with `run --once`, `show-diff`, and `validate-config` subcommands.

When this slice ships:
- A configured machine can run `uv run door-sync run --once` and have a full reconcile cycle execute against real CiviCRM and UniFi endpoints
- A configured machine can run `uv run door-sync run --once --dry-run` and see what would happen without writing
- A configured machine can run `uv run door-sync show-diff` to inspect drift without touching audit/state/alert
- A halted or crashed cycle leaves a machine-readable signal (flag file + audit JSONL entry)
- Every cycle is recorded in `/var/log/door-sync/audit.jsonl`
- `/var/lib/door-sync/state.json` exposes a last-success timestamp for healthchecks

## 2. Definition of done

All three commands green:

```bash
uv run pytest
uv run mypy --strict src tests
uv run ruff check .
```

Plus:
- `src/door_sync/orchestrator.py`, `src/door_sync/audit.py`, `src/door_sync/alert.py`, `src/door_sync/state.py` exist
- `src/door_sync/__main__.py` exposes `run`, `show-diff`, `validate-config` subcommands via `argparse`
- `tests/test_orchestrator.py`, `tests/test_audit.py`, `tests/test_state.py`, `tests/test_alert.py`, `tests/test_main.py` exist with at least the tests enumerated in §10
- `Config` gains an `ops_paths: OpsPaths` attribute (audit JSONL path, state JSON path, alert flag path), with TOML schema validated in `config.py`
- `models.py` gains a frozen `State` dataclass
- `config.example.toml` documents the new `[ops]` section
- `CLAUDE.md` is updated to reflect the subcommand surface
- Architecture §12 is updated: "Audit log entry schema" is removed; "Alerting transport" remains but its row is amended to note the flag-file stub is shipped and the SMTP/webhook decision is what's deferred

## 3. Non-goals (deferred)

- **Daemon mode.** No scheduler, no loop, no `threading.Event.wait`, no SIGTERM handling. `run --once` is required; bare `run` exits with usage error 64. The scheduler is a separate slice.
- **Real alert transport.** No SMTP, no webhook, no Slack. The flag file plus a `logger.error(...)` are the only alerting mechanisms in this slice. The architecture §12 "Alerting transport" item stays open.
- **Audit log rotation.** Logrotate handles this externally. The audit writer must be compatible with `copytruncate` (open-append on each write, no long-lived file handle), but the logrotate config itself is a packaging concern, not a code concern.
- **Audit schema beyond v1.** Future fields (e.g., per-card before/after policy diffs, latency timings) get added incrementally. The schema in §6 is the v1.
- **State migrations.** State is a write-only-with-overwrite JSON file with a fixed shape. If the shape changes, we read the old file, fill in defaults for new fields, and overwrite. No versioning today.
- **Concurrent invocation safety.** Single-process daemon; no file locking, no flock. If two `run --once` invocations race, JSONL appends interleave at line boundaries (which is fine) and state-file `os.replace` is atomic (last writer wins, which is also fine for our purposes).
- **The `webhook` module** from architecture §13 / Appendix C. Out of scope.

## 4. Module layout

```
src/door_sync/
├── orchestrator.py    # new — reconcile(config, *, dry_run) -> ReconcileResult
├── audit.py           # new — log_applied, log_halt, log_crashed
├── alert.py           # new — raise_, clear
├── state.py           # new — read, write_success, write_halt
├── __main__.py        # rewrite — argparse with three subcommands
├── cli.py             # new — pretty-print helpers for show-diff and validate-config
├── config.py          # modify — add OpsPaths, validate [ops] section
├── models.py          # modify — add frozen State dataclass
└── (existing files unchanged)
```

**Strict layering (architecture §4):**

| Module | May import |
|---|---|
| `orchestrator` | `civicrm.client`, `unifi.client`, `reconciler`, `safety`, `tier_mapping`, `audit`, `alert`, `state`, `models`, `config` |
| `audit` | `models` |
| `alert` | (stdlib only) |
| `state` | `models` |
| `cli` | `models` |
| `__main__` | `orchestrator`, `audit`, `alert`, `config`, `cli` |

Nothing imports `orchestrator` except `__main__` (and, eventually, `scheduler` and `webhook`).

## 5. Orchestrator (`orchestrator.py`)

Single public function. The body matches architecture §10 with audit/alert/state wired in.

```python
def reconcile(config: Config, *, dry_run: bool) -> ReconcileResult:
    civicrm = CivicrmClient(config.civicrm)
    unifi = UnifiClient(config.unifi, dry_run=dry_run)
    paths = config.ops_paths

    civi_members = civicrm.fetch_active()
    resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
    unifi_users = unifi.fetch_users()

    diff = reconciler.compute_diff(resolved, unifi_users)
    active_baseline = sum(1 for u in unifi_users if u.active)
    check = safety.check(diff, baseline=active_baseline, thresholds=config.safety)

    if check.halted:
        audit.log_halt(
            check.reason, diff,
            dry_run=dry_run, path=paths.audit_jsonl,
            facility_code=config.unifi.facility_code,
        )
        alert.raise_(check.reason, path=paths.alert_flag)
        if not dry_run:
            state.write_halt(paths.state_json, reason=check.reason)
        return ReconcileResult(halted=True, reason=check.reason, diff=diff)

    unifi.apply(diff)
    audit.log_applied(
        diff, dry_run=dry_run, path=paths.audit_jsonl,
        facility_code=config.unifi.facility_code,
    )
    if not dry_run:
        state.write_success(paths.state_json)
        alert.clear(path=paths.alert_flag)
    return ReconcileResult(halted=False, reason=None, diff=diff)
```

**Invariants:**

- No globals. Everything comes from `config`.
- Clients constructed per cycle. Cheap to instantiate; gives clean isolation.
- The orchestrator owns I/O ordering. Pure modules own correctness.
- Exceptions propagate. `reconcile()` does not catch — `__main__` does (see §9).
- Pure modules behave identically in dry-run and live. The orchestrator and `unifi.apply()` are the only places that branch on `dry_run`.

**Side-effect matrix:**

| Outcome | `unifi.apply` called | audit | state | alert |
|---|---|---|---|---|
| applied, live | yes (writes) | applied line | write_success | clear |
| applied, dry-run | yes (no-op) | applied line, `dry_run=true` | unchanged | unchanged |
| halted, live | no | halted line | write_halt | raise_ |
| halted, dry-run | no | halted line, `dry_run=true` | unchanged | raise_ |
| crashed | (unreachable past raise) | crashed line (from `__main__`) | unchanged | raise_ (from `__main__`) |

Dry-run still fires the alert flag on halts: a halt is information the operator needs whether or not writes were enabled. Dry-run does not touch state because `last_success_iso` must mean "we successfully reconciled prod at least once."

## 6. Audit (`audit.py`)

JSONL file, append-only. One line per cycle outcome. Compatible with logrotate `copytruncate` (open-append on each write; no long-lived handle).

### API

```python
def log_applied(diff: Diff, *, dry_run: bool, path: Path, facility_code: int) -> None
def log_halt(reason: str, diff: Diff, *, dry_run: bool, path: Path, facility_code: int) -> None
def log_crashed(exc: BaseException, *, path: Path) -> None
```

`facility_code` is required for `log_applied`/`log_halt` because the schema (§6) records `nfc_id` hex last-4, and `nfc_id = (facility_code << 16) | card_id`. The orchestrator passes `config.unifi.facility_code`.

### Schema v1

Common fields on every record:

| Field | Type | Notes |
|---|---|---|
| `ts` | string | ISO 8601 UTC, `Z` suffix (e.g., `"2026-05-23T14:32:11Z"`), seconds precision |
| `event` | string | `"applied"` \| `"halted"` \| `"crashed"` |
| `dry_run` | bool | Always present. `false` for `crashed`. |

`applied` and `halted` records add:

| Field | Type | Notes |
|---|---|---|
| `summary` | object | `{added, updated_credential, updated_policy, deactivated, unmapped}` — integer counts |
| `card_last4` | object | `{added: [...], updated_credential: [...], deactivated: [...]}` — list of last-4-hex-chars per affected card |

`halted` records additionally include:

| Field | Type | Notes |
|---|---|---|
| `reason` | string | The `CheckResult.reason` from safety.check |

`crashed` records include only common fields plus:

| Field | Type | Notes |
|---|---|---|
| `exception` | object | `{class: "ConnectionError", message: "..."}` — `repr` is not used (may leak internals) |

### Example records

```json
{"ts":"2026-05-23T14:32:11Z","event":"applied","dry_run":false,"summary":{"added":3,"updated_credential":1,"updated_policy":2,"deactivated":0,"unmapped":0},"card_last4":{"added":["1234","5678","9abc"],"updated_credential":["def0"],"deactivated":[]}}
{"ts":"2026-05-23T14:42:11Z","event":"halted","dry_run":false,"reason":"mass_deactivate exceeded 15% threshold (would deactivate 12 of 50)","summary":{"added":0,"updated_credential":0,"updated_policy":0,"deactivated":12,"unmapped":0},"card_last4":{"added":[],"updated_credential":[],"deactivated":["1234","5678","9abc"]}}
{"ts":"2026-05-23T14:52:11Z","event":"crashed","dry_run":false,"exception":{"class":"ConnectTimeout","message":"timed out connecting to civicrm.example.org"}}
```

(Card last-4 chosen from the card's `nfc_id` hex — same redaction approach as the UniFi client's operational logs, per architecture §11.)

### Writer semantics

- `json.dumps(record, separators=(",", ":"))` + `"\n"` — compact, easy to grep. Field order follows insertion order in the writer (Python 3.7+ guarantees dict insertion order); tests assert on parsed dict equality, not byte equality, so order is not load-bearing.
- `path.parent.mkdir(parents=True, exist_ok=True)` before each write — permissions errors propagate
- `open(path, "a", encoding="utf-8")` per call, write, close. No buffering across calls; no fsync (audit is recovery-tolerant; we accept losing the last line on a hard crash to keep writes fast — state.json uses fsync because it's a single-record file where losing a write matters)
- The `card_last4` lists exclude entries with no card: `to_update_policy` never lists cards (policy-only change), `unmapped` never lists cards (we don't have a UniFi card to redact). For `to_add` / `to_update_credential` / `to_deactivate`, every entry contributes one last-4 string in source order.

## 7. State (`state.py`)

JSON file with four fields. Atomic write via tmp + `os.replace`.

### Dataclass (in `models.py`)

```python
@dataclass(frozen=True)
class State:
    last_success_iso: str | None
    last_halt_iso: str | None
    last_halt_reason: str | None
    run_count: int
```

### API

```python
def read(path: Path) -> State
def write_success(path: Path, *, now: datetime | None = None) -> None
def write_halt(path: Path, reason: str, *, now: datetime | None = None) -> None
```

`now` defaults to `datetime.now(timezone.utc)` and is injectable for testing.

### File format

UTF-8 JSON, 2-space indent, trailing newline:

```json
{
  "last_success_iso": "2026-05-23T14:32:11Z",
  "last_halt_iso": null,
  "last_halt_reason": null,
  "run_count": 142
}
```

### Behavior

- `read(path)` — missing file returns `State(None, None, None, 0)`. Malformed JSON raises (do not silently reset; an operator needs to know).
- `write_success(path, now=)` — `read` current state; construct new `State` with `last_success_iso = now.isoformat with Z`, `run_count + 1`; write atomically.
- `write_halt(path, reason, now=)` — `read` current state; construct new `State` with `last_halt_iso = now`, `last_halt_reason = reason`, `run_count + 1`; write atomically.

### Atomic write

1. `tmp = path.with_suffix(path.suffix + ".tmp")`
2. Write JSON to `tmp` (open, write, fsync, close)
3. `os.replace(tmp, path)` — atomic on POSIX
4. Create parent dir if missing; surface permission errors

### Write trigger table

| Cycle outcome | Writer called | Fields updated |
|---|---|---|
| applied, live | `write_success` | `last_success_iso`, `run_count` |
| halted, live | `write_halt` | `last_halt_iso`, `last_halt_reason`, `run_count` |
| applied, dry-run | (none) | — |
| halted, dry-run | (none) | — |
| crashed | (none) | — |

## 8. Alert (`alert.py`)

Smallest module. Stdlib logging + flag file presence.

### API

```python
def raise_(reason: str, *, path: Path) -> None
def clear(*, path: Path) -> None
```

(Trailing underscore on `raise_` because `raise` is a keyword.)

### Behavior

- `raise_(reason, path)`:
  1. `logging.getLogger("door_sync.alert").error("ALERT: %s", reason)` — visible in systemd journal
  2. Atomic write of `reason + "\n"` to `path` (tmp + `os.replace`) — overwrites any previous reason
  3. Create parent dir if missing; surface permission errors
- `clear(path)`:
  1. `path.unlink(missing_ok=True)` — idempotent, no log emission

### Flag file convention

- Presence = alert active. Absence = no alert.
- Contents = human-readable reason for the most recent active alert. UTF-8, trailing newline.
- External monitoring is expected to:
  - `test -e /var/run/door-sync/alert.flag && cat /var/run/door-sync/alert.flag` to detect halt
  - Separately check `state.json.last_success_iso` age to detect stale daemon

### What this module does NOT do

- No SMTP, no webhook, no rate limiting, no dedup
- No "alert resolved" notification on `clear()` — that's a transport concern
- Does not catch its own exceptions — propagate so the operator sees the failure

## 9. CLI (`__main__.py` + `cli.py`)

Three subcommands via `argparse`. Top-level flags apply to all.

### Surface

```
door-sync [-v/--verbose] [--config PATH] [--env-file PATH] <subcommand> [options]

run            Execute reconciliation cycles
  --once         Run one cycle and exit (REQUIRED for now)
  --dry-run      Compute diff, log to audit, fire alert on halt, do not write to UniFi
show-diff      Read-only: fetch both sides, pretty-print the diff, exit
validate-config  Load config, print issues if any, exit 0/1
```

### Default paths

- `--config` defaults to `/etc/door-sync/config.toml` if it exists, otherwise `./config.toml`
- `--env-file` defaults to `/etc/door-sync/env` if it exists, otherwise `./.env`
- Both can be set explicitly
- The `/etc → ./` fallback is resolved inside each subcommand handler (`cmd_run`, `cmd_show_diff`, `cmd_validate_config`) before calling `config_mod.load`, not in argparse — argparse just records the user's explicit value or `None`

### Logging setup

- `logging.basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)` in `main()`
- `-v/--verbose` bumps to DEBUG
- `show-diff` writes its diff output to stdout so it can be piped/redirected separately from operational logs

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Cycle applied successfully; `validate-config` passed; `show-diff` completed |
| 1 | Cycle halted by safety guards; `validate-config` failed |
| 2 | Cycle crashed (exception escaped the orchestrator); `show-diff` client raised |
| 64 | CLI usage error (argparse default; also: `run` without `--once`) |

### `cmd_run` (the core path)

```python
def cmd_run(args: argparse.Namespace) -> int:
    if not args.once:
        print("daemon mode not yet implemented; pass --once", file=sys.stderr)
        return 64

    try:
        config = config_mod.load(config_path=args.config, env_path=args.env_file)
    except ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1

    try:
        result = orchestrator.reconcile(config, dry_run=args.dry_run)
    except Exception as exc:
        logging.getLogger("door_sync").exception("orchestrator crashed")
        audit.log_crashed(exc, path=config.ops_paths.audit_jsonl)
        alert.raise_(f"crashed: {type(exc).__name__}: {exc}", path=config.ops_paths.alert_flag)
        return 2

    return 1 if result.halted else 0
```

### `cmd_show_diff`

- Loads config (same error path as `cmd_run` — exit 1 on `ConfigError`)
- Constructs `CivicrmClient` and `UnifiClient(dry_run=True)` to be safe (we will never call `.apply`)
- Calls `fetch_active`, `resolve`, `fetch_users`, `compute_diff`
- Calls `cli.print_diff(diff, file=sys.stdout)`
- Catches `Exception` only to log + return 2; does NOT write audit/alert/state

### `cmd_validate_config`

- Calls `config_mod.load(...)`; catches `ConfigError`; calls `cli.print_config_issues(...)`
- Returns 1 if any issues, 0 otherwise

### `cli.py` helpers

```python
def print_diff(diff: Diff, *, file: IO[str]) -> None
def print_config_issues(issues: list[ConfigIssue], *, file: IO[str]) -> None
```

- `print_diff` formats five sections (`ADD`, `UPDATE CREDENTIAL`, `UPDATE POLICY`, `DEACTIVATE`, `UNMAPPED`), each with a header `=== ADD (n) ===` and one line per entry: `<contact_id> <display_name> [card_last4=...] [policy=...]`
- `print_config_issues` formats one line per issue: `<path>: <message>`

### Config additions

In `src/door_sync/config.py`:

```python
@dataclass(frozen=True)
class OpsPaths:
    audit_jsonl: Path
    state_json: Path
    alert_flag: Path

@dataclass(frozen=True)
class Config:
    cadence_seconds: int
    civicrm: CivicrmConfig
    unifi: UnifiConfig
    safety: SafetyThresholds
    tier_mapping: TierMapping
    ops_paths: OpsPaths  # new
```

TOML section, with defaults that match architecture §11:

```toml
[ops]
audit_jsonl = "/var/log/door-sync/audit.jsonl"
state_json  = "/var/lib/door-sync/state.json"
alert_flag  = "/var/run/door-sync/alert.flag"
```

All three keys are optional; defaults are the strings above. `_validate_ops` rejects non-string values. No path-shape validation (relative vs. absolute, existence, writability) — parent-dir creation happens at write time, and a dev checkout often uses relative paths.

## 10. Test plan

Tests mirror source tree under `tests/`. No mocks beyond `monkeypatch` and `pytest`'s `tmp_path`; fakes are plain Python classes that implement the same surface as `CivicrmClient`/`UnifiClient`.

### `tests/test_audit.py` — 8 tests

1. `log_applied` writes one JSON line with `event=applied`, `dry_run=false`, correct `summary` counts
2. `log_applied(dry_run=True)` sets `dry_run=true`; rest of record identical
3. `log_halt` writes `event=halted`, includes `reason`
4. `log_crashed` writes `event=crashed`, `exception={class,message}`, no `summary`
5. `card_last4.added` / `updated_credential` / `deactivated` contain expected last-4-hex strings; `to_update_policy` and `unmapped` do not appear in `card_last4`
6. Two sequential writes append two lines, file remains valid JSONL
7. Missing parent directory is created
8. **Redaction canary**: no value anywhere in the written record contains the full `nfc_id` hex; only last-4 substrings appear

### `tests/test_state.py` — 9 tests

1. `read` on missing file returns `State(None, None, None, 0)`
2. `write_success` from empty state sets `last_success_iso` to injected `now`, increments `run_count` to 1
3. `write_success` preserves existing `last_halt_iso` / `last_halt_reason`
4. `write_halt` sets `last_halt_iso` + `last_halt_reason`, preserves `last_success_iso`, increments `run_count`
5. Round-trip: write → read returns equal `State`
6. Atomic-write canary: after `write_success`, no `.tmp` file remains in the directory
7. Malformed JSON in existing file raises (do not silently reset)
8. `now` defaults to UTC if omitted (assert ISO string ends with `Z`)
9. Missing parent directory is created

### `tests/test_alert.py` — 6 tests

1. `raise_` creates the file with the given reason + newline
2. `raise_` overwrites a previous reason
3. `clear` removes an existing file
4. `clear` on a missing file is idempotent (no exception)
5. `raise_` creates missing parent directory
6. `raise_` emits a logger.error at level ERROR with the reason in the message

### `tests/test_orchestrator.py` — 7 tests

Uses `FakeCivicrmClient` and `FakeUnifiClient` (plain classes with the same surface; no mock library).

1. **Happy path**: fakes return data with no drift → `halted=False`; one audit line with `event=applied`; state file shows `last_success_iso`; alert flag absent
2. **Apply drift**: fakes return data with one add + one deactivate → audit summary reflects counts; `unifi.apply` called once with the computed `Diff`
3. **Safety halt**: fakes return data that triggers `mass_deactivate_pct` → `halted=True`; audit `event=halted` with reason; state shows `last_halt_*`; alert flag present with reason in contents
4. **Idempotency canary**: run `reconcile` twice in a row against fakes that mutate their `fetch_users` return between calls based on what `apply` received; second cycle's `Diff` has all-empty lists
5. **Dry-run apply**: `dry_run=True` with drift → `halted=False`; audit line marked `dry_run=true`; state file unchanged; alert flag absent; `unifi` was constructed with `dry_run=True`
6. **Dry-run halt**: `dry_run=True` + halt-triggering data → audit line marked `dry_run=true, event=halted`; state file unchanged; alert flag DOES appear
7. **Exception propagates**: `FakeCivicrmClient.fetch_active` raises → exception bubbles out of `reconcile()`; no audit/state/alert side-effects (orchestrator does not catch)

### `tests/test_main.py` — 7 tests

Invoke `__main__.main(argv=[...])` directly with `monkeypatch` swapping `orchestrator.reconcile`. Use `tmp_path` for config + ops paths.

1. `run --once` with reconcile returning `halted=False` → exit 0
2. `run --once` with reconcile returning `halted=True` → exit 1
3. `run --once` with reconcile raising → exit 2; audit JSONL has crashed entry; alert flag created
4. `run` without `--once` → exit 64; stderr mentions daemon mode
5. `validate-config` with bad config → exit 1; issues printed to stderr
6. `validate-config` with good config → exit 0
7. `show-diff` with successful fetch → exit 0; diff printed to stdout in five sections; no audit / state / alert touched

## 11. Architecture doc updates (in same PR)

- §12 row "Audit log entry schema": delete (closed by this spec §6)
- §12 row "Alerting transport": amend to read "Flag-file stub is shipped; SMTP/webhook transport TBD"
- §10 orchestrator code block: replace with the version in this spec §5 (adds audit/alert/state wiring), keeping the existing prose intact

## 12. CLAUDE.md updates (in same PR)

Replace the Commands section block:

```bash
uv sync                                  # install
uv run pytest                            # tests
uv run mypy --strict src tests           # type check (strict)
uv run ruff check .                      # lint
uv run door-sync run --once              # one reconcile cycle, exit
uv run door-sync run --once --dry-run    # compute + log diff; no UniFi writes
uv run door-sync show-diff               # read-only: print diff, exit
uv run door-sync validate-config         # load config, print issues, exit
```

Update the status line:

> **Status: in active development.** Pure modules, CiviCRM client, UniFi client, orchestrator + ops stubs (audit, alert, state) are merged. Scheduler (daemon loop, SIGTERM handling) and a real alert transport are the remaining slices.

## 13. Sequencing notes for the implementation plan

Suggested task order (to keep each commit independently testable):

1. `State` dataclass in `models.py` + `tests/test_state.py` (write tests first; lib is so small TDD is natural)
2. `state.py` implementation
3. `audit.py` + `tests/test_audit.py`
4. `alert.py` + `tests/test_alert.py`
5. `OpsPaths` in `config.py` + extended `_validate_ops` + example TOML update
6. `orchestrator.py` + `tests/test_orchestrator.py` (fakes go here)
7. `cli.py` (pretty-printer)
8. `__main__.py` rewrite + `tests/test_main.py`
9. CLAUDE.md update
10. `docs/architecture.md` §10/§12 update

Each step ends green; the orchestrator step is the first one where end-to-end smoke testing against a real Pi becomes possible.
