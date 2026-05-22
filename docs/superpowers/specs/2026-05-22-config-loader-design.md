# Config loader — design

**Date:** 2026-05-22
**Status:** Approved for planning
**Companion:** [`docs/architecture.md`](../../architecture.md) §4 (module table — `config`), §11 (conventions), §12 (deferred items). This spec resolves the "Config schema" deferral.

---

## 1. Goal

Implement `src/door_sync/config.py`: load configuration from a TOML file (non-secrets) and an env file (secrets), validate, and return a frozen `Config` that bundles every setting later slices will need.

When this slice ships:
- `config.load()` returns a fully populated, validated `Config`.
- All validation runs at load time; an invalid config produces a `ConfigError` listing every problem, not just the first.
- The pure-module dataclasses already in `models.py` (`SafetyThresholds`, `TierMapping`, `TierRule`) are reused as-is — no parallel schema.
- `config.toml.example` and `.env.example` at the repo root let a developer copy, edit, and run.

## 2. Definition of done

All three commands green:

```bash
uv run pytest
uv run mypy --strict src tests
uv run ruff check .
```

Plus:

- `config.py` exports `Config`, `CivicrmConfig`, `UnifiConfig`, `ConfigError`, `ConfigIssue`, `load`.
- Every validator (per §5 below) has at least one pass-case and one fail-case test.
- A `tmp_path` integration test writes a complete valid TOML + env pair, calls `load()`, and asserts the returned `Config` is equal to a hand-constructed expected value.
- `config.example.toml` and `.env.example` exist at repo root and parse cleanly via `load()`.
- `config.py` uses **stdlib only** — no `pydantic`, no `dotenv`, no third-party deps. (`tomllib` is stdlib in Python 3.11+.)
- `config.py` is not pure — it does file I/O — but it does NOT call `sys.exit` / `SystemExit`. Errors surface as exceptions; the (future) entry point translates them to exit codes.

## 3. Non-goals (deferred to later slices)

- `AlertConfig` — `alert.py` slice will introduce it. Adding a placeholder now means guessing the shape (SMTP vs webhook).
- CLI flags (`--config-file`, `--env-file`). `__main__.py` is not modified in this slice.
- Config hot-reload / change detection.
- Schema versioning / migration.
- `systemd/` deployment files. The example `.env` is dev-shaped; prod will use systemd's `EnvironmentFile` directive separately.
- Wiring `load()` into `__main__.py` or a scheduler. This slice provides the loader; later slices consume it.

## 4. The Config tree

All frozen dataclasses, defined in `config.py`:

```python
@dataclass(frozen=True)
class CivicrmConfig:
    host: str        # from TOML
    api_key: str     # from env

@dataclass(frozen=True)
class UnifiConfig:
    host: str             # from TOML
    api_key: str          # from env
    tls_fingerprint: str  # from TOML

@dataclass(frozen=True)
class Config:
    cadence_seconds: int
    civicrm: CivicrmConfig
    unifi: UnifiConfig
    safety: SafetyThresholds   # imported from door_sync.models
    tier_mapping: TierMapping  # imported from door_sync.models
```

`SafetyThresholds` and `TierMapping`/`TierRule` are the existing dataclasses from `models.py`. `config.load()` constructs them from validated TOML data and embeds them in the returned `Config`. The pure modules continue consuming the same dataclass types they already accept — no conversion layer needed downstream.

## 5. File formats

### 5.1 TOML (`config.toml`)

```toml
cadence_seconds = 600

[civicrm]
host = "https://civicrm.example.org"

[unifi]
host = "https://unifi.example.org:12445"
tls_fingerprint = "AA:BB:CC:DD:EE:FF:11:22:33:44:55:66:77:88:99:00:AA:BB:CC:DD:EE:FF:11:22:33:44:55:66:77:88:99:00"

[safety]
mass_deactivate_pct = 0.15
mass_add_pct = 0.25
mass_policy_pct = 0.20
baseline_floor = 10

[tier_mapping.rules.Gold]
resolution = "tier"
target_policy = "policy-abc-123"
rank = 100

[tier_mapping.rules.Comp]
resolution = "none"
rank = 50

[tier_mapping.rules."Day Pass"]
resolution = "day-pass"
rank = 10
```

The TOML quoted-key form (`"Day Pass"`) handles membership-type names with spaces, hyphens, or other non-bareword characters.

### 5.2 Env (`.env`)

```
CIVICRM_API_KEY=...
UNIFI_API_KEY=...
```

Same format as systemd's `EnvironmentFile` directive: `KEY=value` per line, `#` comments, blank lines, optional double or single quotes around the value. No shell expansion, no `export` prefix, no line continuations.

## 6. Loading API

```python
def load(
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """Load and validate config from TOML + env.

    Path resolution per argument:
      1. Explicit argument (if not None)
      2. {DOOR_SYNC_CONFIG_DIR}/config.toml or {DOOR_SYNC_CONFIG_DIR}/env if env var is set
      3. ./config.toml and ./.env (cwd)

    Raises:
      ConfigError: if validation fails (one or more issues collected).
                   Missing TOML file is reported as a single issue.
                   Missing .env file is fine; falls back to os.environ.
    """
```

`config_path` and `env_path` are keyword-only so callers can opt into just one without positional confusion. The function is the single public entry point; nothing in the slice exposes a "load TOML only" or "load env only" function.

## 7. Loading flow

1. Resolve `config_path` (explicit → env-var → default).
2. Resolve `env_path` likewise.
3. Read TOML: `tomllib.load()`. On `FileNotFoundError`, append a single `ConfigIssue` with `path="config_file"` and raise `ConfigError`.
4. Load env file via the private `_load_env_file(path) -> dict[str, str]`. If file missing, return `{}` silently.
5. Build env source chain: a function `env_get(name) -> str | None` that returns the env-file value if present, else `os.environ.get(name)`. File wins so dev-mode `.env` overrides any stray shell vars; in prod, systemd has already populated `os.environ` with the same content so precedence doesn't matter.
6. Walk the TOML structure with field-by-field validators (§8), collecting issues into `list[ConfigIssue]`.
7. Pull required env vars (`CIVICRM_API_KEY`, `UNIFI_API_KEY`); validate non-empty after `strip()`.
8. If `issues` non-empty → `raise ConfigError(issues)`.
9. Construct the dataclass tree:
   - `SafetyThresholds(**validated_safety_dict)`
   - `TierMapping(rules={name: TierRule(**rule) for name, rule in validated_rules.items()})`
   - Wrap in `Config(...)` and return.

## 8. Validators

| Field | Rule |
|---|---|
| `cadence_seconds` | `int`, `>= 60` (prevent accidental tight polling loops) |
| `civicrm.host` | non-empty `str`, starts with `https://` |
| `unifi.host` | non-empty `str`, starts with `https://` |
| `unifi.tls_fingerprint` | regex `^([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$\|^[0-9A-Fa-f]{64}$` (SHA-256, with or without colon separators) |
| `safety.mass_deactivate_pct` | `float`, `0 < x <= 1` |
| `safety.mass_add_pct` | `float`, `0 < x <= 1` |
| `safety.mass_policy_pct` | `float`, `0 < x <= 1` |
| `safety.baseline_floor` | `int`, `>= 0` |
| `tier_mapping.rules.<Name>.resolution` | `str` in `{"tier", "none", "day-pass"}` |
| `tier_mapping.rules.<Name>.target_policy` | When `resolution == "tier"`: key must be present, non-empty `str`. When `resolution != "tier"`: key must be **omitted** (TOML 1.0 has no `null`/`None` literal). For non-tier rules, the constructed `TierRule.target_policy` is `None`. |
| `tier_mapping.rules.<Name>.rank` | `int` |
| `CIVICRM_API_KEY` | non-empty `str` after `.strip()` |
| `UNIFI_API_KEY` | non-empty `str` after `.strip()` |

**Defaults** baked into the validators (used when TOML key is absent, not present-but-invalid):

- `cadence_seconds = 600`
- `safety.mass_deactivate_pct = 0.15`
- `safety.mass_add_pct = 0.25`
- `safety.mass_policy_pct = 0.20`
- `safety.baseline_floor = 10`

(These match `SafetyThresholds`' dataclass defaults from `models.py`. The redundancy is intentional: validators apply defaults during TOML walking, then construct the dataclass with full kwargs. This keeps the validation logic explicit at one site.)

All other fields are required — no default makes sense for `civicrm.host`, `unifi.tls_fingerprint`, etc. The `tier_mapping.rules` table itself is optional (empty mapping is valid TOML; it means every member is unmapped, which the safety guard will halt on — useful first-run behavior).

## 9. Errors

```python
@dataclass(frozen=True)
class ConfigIssue:
    path: str       # e.g. "safety.mass_deactivate_pct" or "tier_mapping.rules.Gold.rank"
    message: str    # e.g. "must be between 0 and 1, got 1.5"


class ConfigError(Exception):
    issues: list[ConfigIssue]

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        super().__init__(self._format())

    def _format(self) -> str:
        lines = ["Configuration errors:"]
        lines.extend(f"  {i.path}: {i.message}" for i in self.issues)
        return "\n".join(lines)
```

The collect-then-raise pattern means an operator with three typos sees all three in one run, not iteratively. Mirrors pydantic's `ValidationError` behavior without the dependency.

`config.py` never calls `sys.exit`. The future entry point (likely in `__main__.py` or `scheduler.py`) will catch `ConfigError` and exit with code 2. This keeps `config.py` unit-testable without subprocess machinery.

## 10. The `.env` parser

Inline private helper in `config.py`, ~25 lines:

```python
def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=value file. Returns {} if path doesn't exist."""
```

Supported:
- `KEY=value` (the common case)
- `KEY="value with spaces"` (double-quoted; strip the quotes)
- `KEY='value'` (single-quoted; strip the quotes)
- `# comment` lines
- Blank lines
- Whitespace around `=` (trimmed)

Rejected (parser raises `ValueError` with a line-number message; `load()` catches and appends a `ConfigIssue` with `path="env_file"` to the running issues list, then continues with `env = {}`):
- Lines that aren't `KEY=value`, `#...`, or blank
- Unclosed quotes
- Empty KEY (`=value`)

NOT supported (no parsing attempted; treated as literal):
- `$VAR` expansion
- Command substitution `$(...)` / backticks
- `export KEY=value` prefix
- Line continuations (`\` at end of line)
- Multiline values

This matches the simple subset of systemd's `EnvironmentFile` directive, so the same file can be used by both dev (Python parses it directly) and prod (systemd parses it and passes via process env). systemd supports a slightly richer syntax (shell-like quoting, some escaping) that we deliberately do not implement; if a file works for our parser it will also work for systemd, but not vice-versa.

**Why the parser doesn't import `ConfigError`:** `_load_env_file` is a private utility with a narrow job. `ValueError` is a stdlib exception with no project coupling; `load()` is the only caller and the natural place to wrap it into a `ConfigIssue`. This keeps the parser unit-testable in isolation.

## 11. Test plan

`tests/test_config.py`:

**`.env` parser:**
- Empty file → `{}`
- Comment-only file → `{}`
- Single `KEY=value` line → `{"KEY": "value"}`
- Quoted values (single and double) → quotes stripped
- Whitespace around `=` → trimmed
- Blank lines + comments mixed with real entries → only the entries returned
- Malformed line (no `=`) → `_load_env_file` raises `ValueError`; from `load()`, surfaces as `ConfigError` with `path="env_file"`
- Empty key (`=value`) → same
- File doesn't exist → `{}` (no error)

**Per-validator pass/fail:**
- `cadence_seconds`: 60 passes, 59 fails, "abc" fails, missing → default 600
- `civicrm.host` / `unifi.host`: `https://...` passes, `http://...` fails, `""` fails
- `tls_fingerprint`: 64-hex passes, colon-separated 32-byte passes, 63-hex fails, garbage fails
- `mass_*_pct`: 0.5 passes, 0 fails, 1.0 passes, 1.5 fails, "abc" fails
- `baseline_floor`: 0 passes, -1 fails, "abc" fails
- Tier rule: `resolution="tier"` with `target_policy` passes; `resolution="tier"` without `target_policy` fails; `resolution="none"` with `target_policy` fails; `resolution="xyz"` fails

**Error collection:**
- TOML with 3 distinct invalid fields → `ConfigError.issues` has all 3 with correct paths

**Env precedence:**
- `.env` has `CIVICRM_API_KEY=from_file`, `os.environ` has `CIVICRM_API_KEY=from_environ` → file wins
- `.env` lacks the key, `os.environ` has it → uses environ value
- Neither has it → `ConfigError`

**Path resolution:**
- Explicit `config_path` arg → uses it (test with `tmp_path`)
- No arg, `DOOR_SYNC_CONFIG_DIR` set → uses `{dir}/config.toml`
- No arg, no env var → uses `./config.toml`

**Integration:**
- `tmp_path/config.toml` + `tmp_path/env` written by the test with a full valid config
- `load(config_path=..., env_path=...)` returns a `Config` equal to a hand-constructed expected value
- `Config.safety` is a `SafetyThresholds` instance (the `models.py` dataclass)
- `Config.tier_mapping.rules["Gold"]` is a `TierRule` instance (the `models.py` dataclass)

**Example files:**
- `load()` against `config.example.toml` + `.env.example` (with stub API keys) parses without error. Catches drift between examples and real schema.

All tests use pytest's `tmp_path` for file-based cases and `monkeypatch` for env-var manipulation. No global `os.environ` mutation.

## 12. Example files

### 12.1 `config.example.toml`
A complete, commented, valid TOML at repo root. Documents every supported field, including defaults explicitly written out so readers see the shape. Useful as a copy-and-edit starting point.

### 12.2 `.env.example`
```
# Copy to .env and fill in real values.
# Mode 0400 in production (/etc/door-sync/env).
CIVICRM_API_KEY=replace-me
UNIFI_API_KEY=replace-me
```

Both files added to git. (`.env` itself stays gitignored.)

## 13. Things explicitly NOT decided here

- **Whether `cadence_seconds` should also enforce an upper bound.** Currently lower-bound only (`>= 60`). Operators with infrequent updates might legitimately want hours; not the loader's call to forbid that.
- **Whether to validate that `civicrm.host` and `unifi.host` are well-formed URLs beyond the `https://` prefix.** Stdlib's `urllib.parse.urlparse` is loose. A real malformed URL will fail at HTTP-client time with a clear error; pre-validating it here adds code for marginal benefit.
- **TOML schema versioning.** No `schema_version` field in v1. If we ever break the schema, the safest path is renaming the file (`config.v2.toml`).
- **Whether `.env.example` should ship in `.gitignore` exceptions.** Yes by default — it's the example, not the real one.

## 14. Risks

- **Hand-rolled `.env` parser drift from systemd's `EnvironmentFile`.** Mitigation: stay strict (reject anything we don't explicitly support), test the supported cases exhaustively, and document the matching subset in the parser docstring.
- **Strict `https://` requirement may bite a developer testing against `http://localhost`.** Mitigation: noted in `config.example.toml`; can be relaxed if it becomes friction. The architecture (§12, security) calls for TLS verification on production paths, so this is the right default.
- **Schema duplication between `config.py` validators and `models.py` defaults.** The defaults appear in both places (e.g., `mass_deactivate_pct = 0.15`). Mitigation: the test that loads `config.example.toml` catches drift if either side changes without the other.
