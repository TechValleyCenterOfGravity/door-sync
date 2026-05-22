# Config Loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `src/door_sync/config.py` so `load()` reads a TOML file (non-secrets) plus an env file (secrets), validates everything in one pass, and returns a frozen `Config` reusing the existing `models.py` dataclasses.

**Architecture:** Stdlib only (`tomllib` + `dataclasses` + `re` + `pathlib`). Validators collect issues into a list rather than raising; `load()` raises a single `ConfigError` containing every problem at the end. A private `_load_env_file()` parses a `KEY=value` file (matching the simple subset of systemd's `EnvironmentFile`). Pure-module dataclasses (`SafetyThresholds`, `TierMapping`, `TierRule`) are imported from `models.py` and embedded directly in `Config` — no parallel schema.

**Tech Stack:** Python 3.11+ (for `tomllib`), `uv` for env/scripts, `pytest` 9, `mypy --strict`, `ruff`. No new runtime deps.

**Spec:** [`docs/superpowers/specs/2026-05-22-config-loader-design.md`](../specs/2026-05-22-config-loader-design.md). Read it first.

**Conventions (architecture §11):**
- Type hints on every function. `mypy --strict src tests` must be green.
- Imports ordered: stdlib → third-party → `door_sync.*`. No `from x import *`.
- `config.py` does file I/O — it is NOT a pure module — but it does NOT call `sys.exit`.
- Errors surface as `ConfigError`, never as `SystemExit`.

**Verification commands** (used at the end of every task):

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

All three must pass before moving to the next task.

---

## Task 1: Data shapes — `Config`, `CivicrmConfig`, `UnifiConfig`, `ConfigIssue`, `ConfigError`

**Files:**
- Create: `src/door_sync/config.py` (data shapes only — no `load()` yet)
- Create: `tests/test_config.py` (frozen + format tests)

### Background

Build the data contracts first so the loader and tests have something to construct. No logic in this task — just the frozen dataclasses and the exception.

`ConfigError` collects multiple `ConfigIssue`s so an operator with three typos sees all three at once. Its `__str__` formats them as a bulleted list.

- [ ] **Step 1.1: Write `tests/test_config.py` — the full file**

```python
from dataclasses import FrozenInstanceError

import pytest

from door_sync.config import (
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    UnifiConfig,
)
from door_sync.models import SafetyThresholds, TierMapping


def test_civicrm_config_is_frozen() -> None:
    c = CivicrmConfig(host="https://x", api_key="k")
    with pytest.raises(FrozenInstanceError):
        c.host = "https://y"  # type: ignore[misc]


def test_unifi_config_is_frozen() -> None:
    u = UnifiConfig(host="https://x", api_key="k", tls_fingerprint="AB" * 32)
    with pytest.raises(FrozenInstanceError):
        u.api_key = "z"  # type: ignore[misc]


def test_config_is_frozen() -> None:
    c = Config(
        cadence_seconds=600,
        civicrm=CivicrmConfig(host="https://x", api_key="k"),
        unifi=UnifiConfig(host="https://y", api_key="k", tls_fingerprint="AB" * 32),
        safety=SafetyThresholds(),
        tier_mapping=TierMapping(rules={}),
    )
    with pytest.raises(FrozenInstanceError):
        c.cadence_seconds = 60  # type: ignore[misc]


def test_config_issue_is_frozen() -> None:
    i = ConfigIssue(path="x.y", message="bad")
    with pytest.raises(FrozenInstanceError):
        i.message = "good"  # type: ignore[misc]


def test_config_error_stores_issues() -> None:
    issues = [
        ConfigIssue(path="a", message="m1"),
        ConfigIssue(path="b", message="m2"),
    ]
    err = ConfigError(issues)
    assert err.issues == issues


def test_config_error_str_lists_all_issues() -> None:
    issues = [
        ConfigIssue(path="a", message="m1"),
        ConfigIssue(path="b", message="m2"),
    ]
    err = ConfigError(issues)
    text = str(err)
    assert "Configuration errors:" in text
    assert "a: m1" in text
    assert "b: m2" in text


def test_config_error_with_no_issues_still_constructs() -> None:
    err = ConfigError([])
    assert err.issues == []
    assert "Configuration errors:" in str(err)
```

- [ ] **Step 1.2: Run pytest to verify failure**

Run: `uv run pytest tests/test_config.py -v`

Expected: collection error / ImportError — `door_sync.config` does not exist.

- [ ] **Step 1.3: Write `src/door_sync/config.py` — initial version with only data shapes**

```python
"""Config loading and validation for door-sync.

Reads non-secret settings from TOML and secrets from a KEY=value env file.
Returns a frozen Config with the pure-module dataclasses embedded.

This module is not pure (it does file I/O), but it does NOT call sys.exit.
Errors surface as ConfigError so callers can format and exit on their own terms.
"""

from dataclasses import dataclass

from door_sync.models import SafetyThresholds, TierMapping


@dataclass(frozen=True)
class CivicrmConfig:
    host: str
    api_key: str


@dataclass(frozen=True)
class UnifiConfig:
    host: str
    api_key: str
    tls_fingerprint: str


@dataclass(frozen=True)
class Config:
    cadence_seconds: int
    civicrm: CivicrmConfig
    unifi: UnifiConfig
    safety: SafetyThresholds
    tier_mapping: TierMapping


@dataclass(frozen=True)
class ConfigIssue:
    path: str
    message: str


class ConfigError(Exception):
    """Raised when configuration loading or validation produces one or more issues."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = list(issues)
        super().__init__(self._format())

    def _format(self) -> str:
        lines = ["Configuration errors:"]
        lines.extend(f"  {i.path}: {i.message}" for i in self.issues)
        return "\n".join(lines)
```

- [ ] **Step 1.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 7 new tests pass (plus the 50 from prior slices = 57 total); mypy success; ruff clean.

- [ ] **Step 1.5: Commit**

```bash
git add src/door_sync/config.py tests/test_config.py
git commit -m "Add Config dataclasses and ConfigError shell"
```

---

## Task 2: `.env` parser

**Files:**
- Modify: `src/door_sync/config.py` (add `_load_env_file` private helper)
- Modify: `tests/test_config.py` (add parser tests)

### Background

`_load_env_file(path) -> dict[str, str]` parses `KEY=value` lines, ignores comments and blanks, strips matching quotes around values, and raises `ValueError` on the first malformed line. Missing file returns `{}`.

Supported:
- `KEY=value`
- `KEY="quoted value"` and `KEY='quoted'` (matched quotes stripped)
- `# comments` and blank lines
- Whitespace around `=`

Rejected with `ValueError`:
- Lines that aren't `KEY=value`, `#...`, or blank (no `=`)
- Empty key (`=value`)
- Mismatched quotes (`"value` or `value"`)

The parser does NOT support `$VAR` expansion, command substitution, `export` prefix, line continuations, or multiline values.

- [ ] **Step 2.1: Update imports and append parser tests to `tests/test_config.py`**

First, replace the import block at the top of `tests/test_config.py` with:

```python
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from door_sync.config import (
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    UnifiConfig,
    _load_env_file,
)
from door_sync.models import SafetyThresholds, TierMapping
```

Then append the following tests **after** the existing tests:

```python
# --- _load_env_file tests ---


def test_env_file_missing_returns_empty(tmp_path: Path) -> None:
    result = _load_env_file(tmp_path / "does-not-exist")
    assert result == {}


def test_env_file_empty(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("")
    assert _load_env_file(p) == {}


def test_env_file_single_pair(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("KEY=value\n")
    assert _load_env_file(p) == {"KEY": "value"}


def test_env_file_double_quoted_value(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text('KEY="hello world"\n')
    assert _load_env_file(p) == {"KEY": "hello world"}


def test_env_file_single_quoted_value(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("KEY='hello'\n")
    assert _load_env_file(p) == {"KEY": "hello"}


def test_env_file_strips_whitespace_around_equals(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("KEY =  value\n")
    assert _load_env_file(p) == {"KEY": "value"}


def test_env_file_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("# top comment\n\nKEY=value\n  # indented comment\nOTHER=x\n\n")
    assert _load_env_file(p) == {"KEY": "value", "OTHER": "x"}


def test_env_file_allows_empty_value(tmp_path: Path) -> None:
    # Empty value is allowed; callers decide whether empty = missing.
    p = tmp_path / "env"
    p.write_text("KEY=\n")
    assert _load_env_file(p) == {"KEY": ""}


def test_env_file_malformed_no_equals_raises(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("VALID=ok\nNOTAVALIDLINE\n")
    with pytest.raises(ValueError, match="line 2"):
        _load_env_file(p)


def test_env_file_empty_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("=value\n")
    with pytest.raises(ValueError, match="line 1"):
        _load_env_file(p)


def test_env_file_unclosed_double_quote_raises(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text('KEY="hello\n')
    with pytest.raises(ValueError, match="line 1"):
        _load_env_file(p)


def test_env_file_unclosed_single_quote_raises(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text("KEY=hello'\n")
    with pytest.raises(ValueError, match="line 1"):
        _load_env_file(p)
```

- [ ] **Step 2.2: Run pytest to verify failure**

Run: `uv run pytest tests/test_config.py -v`

Expected: collection error — `_load_env_file` not importable yet.

- [ ] **Step 2.3: Add `_load_env_file` to `src/door_sync/config.py`**

Add this function **after** the `ConfigError` class (at the bottom of `config.py`):

```python
from pathlib import Path


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=value file. Returns {} if path doesn't exist.

    Supports: KEY=value, KEY="quoted", KEY='quoted', # comments, blank lines,
    whitespace around =. Matches the simple subset of systemd's EnvironmentFile.

    Raises ValueError on the first malformed line (with the 1-based line number
    in the message). Empty value is allowed; an empty key is not.
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(
                f"line {line_no}: not KEY=value, comment, or blank: {raw_line!r}"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"line {line_no}: empty key: {raw_line!r}")
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        elif value and (value[0] in ('"', "'") or value[-1] in ('"', "'")):
            raise ValueError(f"line {line_no}: unclosed quote: {raw_line!r}")
        result[key] = value
    return result
```

Note: the `from pathlib import Path` should move to the top import block. Place it after `from dataclasses import dataclass` so the final import order is:

```python
from dataclasses import dataclass
from pathlib import Path

from door_sync.models import SafetyThresholds, TierMapping
```

- [ ] **Step 2.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 12 new parser tests pass (19 in test_config.py + 50 elsewhere = 69 total); mypy success; ruff clean.

- [ ] **Step 2.5: Commit**

```bash
git add src/door_sync/config.py tests/test_config.py
git commit -m "Add .env parser to config module"
```

---

## Task 3: Validators + `load()`

**Files:**
- Modify: `src/door_sync/config.py` (add validators, path resolution, `load()`)
- Modify: `tests/test_config.py` (add validator + load tests)

### Background

This is the bulk of the loader. Five validator functions (`_validate_cadence`, `_validate_civicrm`, `_validate_unifi`, `_validate_safety`, `_validate_tier_mapping`) each take the parsed TOML dict and a mutable `issues` list, append `ConfigIssue`s on failure, and return a (possibly stub) value. `load()` orchestrates: parse paths, parse TOML, parse env, walk validators, raise `ConfigError` if `issues` non-empty, otherwise construct and return `Config`.

Defaults applied during validation:
- `cadence_seconds = 600`
- `safety.mass_deactivate_pct = 0.15`, `mass_add_pct = 0.25`, `mass_policy_pct = 0.20`, `baseline_floor = 10`

These match `SafetyThresholds`'s dataclass defaults. The duplication is intentional: the validator owns the "what shows up when TOML key is absent" decision, and the dataclass owns the "what shows up when the dataclass is constructed empty" decision. The drift test in Task 4 (loading `config.example.toml`) catches divergence.

### Sub-cycle A: Path resolution

- [ ] **Step 3.1: Update imports and append path-resolution tests**

First, add `load` to the `from door_sync.config import (...)` block at the top of `tests/test_config.py`:

```python
from door_sync.config import (
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    UnifiConfig,
    _load_env_file,
    load,
)
```

Then append to `tests/test_config.py`:

```python
# --- path resolution tests ---


def test_explicit_paths_override_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg = tmp_path / "custom.toml"
    env = tmp_path / "custom-env"
    cfg.write_text(
        'cadence_seconds = 600\n'
        '[civicrm]\nhost = "https://c"\n'
        '[unifi]\nhost = "https://u"\n'
        'tls_fingerprint = "' + "AB" * 32 + '"\n'
    )
    env.write_text("CIVICRM_API_KEY=x\nUNIFI_API_KEY=y\n")
    result = load(config_path=cfg, env_path=env)
    assert result.civicrm.host == "https://c"


def test_env_var_dir_supplies_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text(
        'cadence_seconds = 600\n'
        '[civicrm]\nhost = "https://c"\n'
        '[unifi]\nhost = "https://u"\n'
        'tls_fingerprint = "' + "AB" * 32 + '"\n'
    )
    (tmp_path / "env").write_text("CIVICRM_API_KEY=x\nUNIFI_API_KEY=y\n")
    monkeypatch.setenv("DOOR_SYNC_CONFIG_DIR", str(tmp_path))
    result = load()
    assert result.civicrm.host == "https://c"


def test_missing_toml_file_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CIVICRM_API_KEY", raising=False)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        load(config_path=tmp_path / "missing.toml", env_path=tmp_path / "missing-env")
    paths = [i.path for i in exc.value.issues]
    assert "config_file" in paths
```

- [ ] **Step 3.2: Sketch the `load()` shell and `_resolve_paths` helper to make these tests pass**

Add to `src/door_sync/config.py`, **after** `_load_env_file`. Also add `import os` and `import tomllib` to the top stdlib import block.

```python
import os
import tomllib


def _resolve_paths(
    config_path: Path | None, env_path: Path | None
) -> tuple[Path, Path]:
    config_dir = os.environ.get("DOOR_SYNC_CONFIG_DIR")
    if config_path is None:
        config_path = (
            Path(config_dir) / "config.toml" if config_dir else Path("config.toml")
        )
    if env_path is None:
        env_path = Path(config_dir) / "env" if config_dir else Path(".env")
    return config_path, env_path


def load(
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """Load and validate config from TOML + env. See module docstring for details."""
    config_path, env_path = _resolve_paths(config_path, env_path)
    issues: list[ConfigIssue] = []

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        issues.append(
            ConfigIssue(path="config_file", message=f"file not found: {config_path}")
        )
        raise ConfigError(issues)
    except tomllib.TOMLDecodeError as e:
        issues.append(ConfigIssue(path="config_file", message=f"invalid TOML: {e}"))
        raise ConfigError(issues)

    # Validators land in step 3.4. For now, just verify TOML loaded.
    raise NotImplementedError("validators not yet implemented")
```

- [ ] **Step 3.3: Run the path-resolution tests to confirm 2 of 3 pass**

Run: `uv run pytest tests/test_config.py -v -k "explicit_paths or env_var_dir or missing_toml"`

Expected:
- `test_missing_toml_file_raises_config_error` → PASS (TOML missing produces the right ConfigError before NotImplementedError fires)
- `test_explicit_paths_override_defaults` → FAIL with `NotImplementedError`
- `test_env_var_dir_supplies_defaults` → FAIL with `NotImplementedError`

That's expected. The other two will pass once the validators land in step 3.4.

### Sub-cycle B: Validators and complete `load()`

- [ ] **Step 3.4: Append validator and integration tests to `tests/test_config.py`**

```python
# --- validator tests ---


def _write_minimal_valid(tmp_path: Path) -> tuple[Path, Path]:
    cfg = tmp_path / "config.toml"
    env = tmp_path / "env"
    cfg.write_text(
        "cadence_seconds = 600\n"
        "[civicrm]\n"
        'host = "https://civi.example.org"\n'
        "[unifi]\n"
        'host = "https://unifi.example.org"\n'
        'tls_fingerprint = "' + ("AB:" * 31 + "AB") + '"\n'
    )
    env.write_text("CIVICRM_API_KEY=civikey\nUNIFI_API_KEY=unifikey\n")
    return cfg, env


def test_load_happy_path_returns_populated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text()
        + "[safety]\nmass_deactivate_pct = 0.10\n"
        + '[tier_mapping.rules.Gold]\n'
        + 'resolution = "tier"\ntarget_policy = "P_GOLD"\nrank = 100\n'
    )
    result = load(config_path=cfg, env_path=env)
    assert result.cadence_seconds == 600
    assert result.civicrm.host == "https://civi.example.org"
    assert result.civicrm.api_key == "civikey"
    assert result.unifi.host == "https://unifi.example.org"
    assert result.unifi.api_key == "unifikey"
    assert result.unifi.tls_fingerprint == "AB:" * 31 + "AB"
    assert result.safety.mass_deactivate_pct == 0.10
    assert result.safety.mass_add_pct == 0.25  # default
    assert "Gold" in result.tier_mapping.rules
    assert result.tier_mapping.rules["Gold"].resolution == "tier"
    assert result.tier_mapping.rules["Gold"].target_policy == "P_GOLD"
    assert result.tier_mapping.rules["Gold"].rank == 100


@pytest.mark.parametrize(
    "cadence_value, expected_ok",
    [
        (60, True),
        (600, True),
        (59, False),
        (0, False),
        (-1, False),
        ('"not-an-int"', False),  # TOML string
    ],
)
def test_cadence_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cadence_value: object,
    expected_ok: bool,
) -> None:
    """Each value is rendered into TOML as-is. Strings must be pre-quoted by the caller."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg_text = cfg.read_text().replace(
        "cadence_seconds = 600", f"cadence_seconds = {cadence_value}"
    )
    cfg.write_text(cfg_text)
    if expected_ok:
        result = load(config_path=cfg, env_path=env)
        assert result.cadence_seconds == cadence_value
    else:
        with pytest.raises(ConfigError) as exc:
            load(config_path=cfg, env_path=env)
        assert any(i.path == "cadence_seconds" for i in exc.value.issues)


def test_cadence_rejects_boolean_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOML 'true' parses to Python True (a bool), which our validator rejects as not-an-int."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text().replace("cadence_seconds = 600", "cadence_seconds = true")
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(i.path == "cadence_seconds" for i in exc.value.issues)


def test_cadence_default_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(cfg.read_text().replace("cadence_seconds = 600\n", ""))
    result = load(config_path=cfg, env_path=env)
    assert result.cadence_seconds == 600


@pytest.mark.parametrize(
    "host, expected_ok",
    [
        ("https://example.org", True),
        ("https://example.org:8080", True),
        ("http://example.org", False),
        ("example.org", False),
        ("", False),
    ],
)
def test_civicrm_host_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected_ok: bool,
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(cfg.read_text().replace("https://civi.example.org", host))
    if expected_ok:
        result = load(config_path=cfg, env_path=env)
        assert result.civicrm.host == host
    else:
        with pytest.raises(ConfigError) as exc:
            load(config_path=cfg, env_path=env)
        assert any(i.path == "civicrm.host" for i in exc.value.issues)


@pytest.mark.parametrize(
    "host, expected_ok",
    [
        ("https://example.org:12445", True),
        ("http://example.org", False),
        ("", False),
    ],
)
def test_unifi_host_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected_ok: bool,
) -> None:
    """Mirror of civicrm.host validation; the two validators are separate functions."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(cfg.read_text().replace("https://unifi.example.org", host))
    if expected_ok:
        result = load(config_path=cfg, env_path=env)
        assert result.unifi.host == host
    else:
        with pytest.raises(ConfigError) as exc:
            load(config_path=cfg, env_path=env)
        assert any(i.path == "unifi.host" for i in exc.value.issues)


@pytest.mark.parametrize(
    "fingerprint, expected_ok",
    [
        ("AB" * 32, True),                          # 64 hex chars
        ("ab" * 32, True),                          # lowercase
        ("AB:" * 31 + "AB", True),                  # colon-separated
        ("AB" * 31, False),                         # 62 chars — too short
        ("XYZ" + "AB" * 31, False),                 # non-hex
        ("", False),
    ],
)
def test_tls_fingerprint_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fingerprint: str,
    expected_ok: bool,
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(cfg.read_text().replace("AB:" * 31 + "AB", fingerprint))
    if expected_ok:
        result = load(config_path=cfg, env_path=env)
        assert result.unifi.tls_fingerprint == fingerprint
    else:
        with pytest.raises(ConfigError) as exc:
            load(config_path=cfg, env_path=env)
        assert any(i.path == "unifi.tls_fingerprint" for i in exc.value.issues)


@pytest.mark.parametrize(
    "pct_value, expected_ok",
    [
        ("0.01", True),
        ("0.5", True),
        ("1.0", True),
        ("0.0", False),
        ("-0.1", False),
        ("1.01", False),
        ('"oops"', False),  # TOML string
    ],
)
def test_safety_pct_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pct_value: str,
    expected_ok: bool,
) -> None:
    """Each value is rendered into TOML as-is. Strings must be pre-quoted."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text() + f"[safety]\nmass_deactivate_pct = {pct_value}\n"
    )
    if expected_ok:
        result = load(config_path=cfg, env_path=env)
        assert result.safety.mass_deactivate_pct == float(pct_value)
    else:
        with pytest.raises(ConfigError) as exc:
            load(config_path=cfg, env_path=env)
        assert any(
            i.path == "safety.mass_deactivate_pct" for i in exc.value.issues
        )


def test_baseline_floor_validation_passes_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(cfg.read_text() + "[safety]\nbaseline_floor = 0\n")
    result = load(config_path=cfg, env_path=env)
    assert result.safety.baseline_floor == 0


def test_baseline_floor_validation_rejects_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(cfg.read_text() + "[safety]\nbaseline_floor = -1\n")
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(i.path == "safety.baseline_floor" for i in exc.value.issues)


def test_tier_rule_tier_requires_target_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text()
        + '[tier_mapping.rules.Gold]\nresolution = "tier"\nrank = 1\n'
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "tier_mapping.rules.Gold.target_policy"
        for i in exc.value.issues
    )


def test_tier_rule_non_tier_forbids_target_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text()
        + '[tier_mapping.rules.Comp]\n'
        + 'resolution = "none"\ntarget_policy = "P_SHOULD_NOT_BE_HERE"\nrank = 1\n'
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "tier_mapping.rules.Comp.target_policy"
        for i in exc.value.issues
    )


def test_tier_rule_invalid_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text()
        + '[tier_mapping.rules.Weird]\nresolution = "xyz"\nrank = 1\n'
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "tier_mapping.rules.Weird.resolution"
        for i in exc.value.issues
    )


def test_tier_mapping_empty_rules_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    # No [tier_mapping.rules.*] tables — valid TOML, empty mapping
    result = load(config_path=cfg, env_path=env)
    assert result.tier_mapping.rules == {}


def test_load_collects_multiple_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CIVICRM_API_KEY", raising=False)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    cfg = tmp_path / "config.toml"
    env = tmp_path / "env"
    cfg.write_text(
        "cadence_seconds = 10\n"  # too low
        "[civicrm]\n"
        'host = "http://no-tls"\n'  # http not https
        "[unifi]\n"
        'host = "https://ok"\n'
        'tls_fingerprint = "not-a-fingerprint"\n'  # bad
    )
    env.write_text("")  # missing both API keys
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    paths = {i.path for i in exc.value.issues}
    assert "cadence_seconds" in paths
    assert "civicrm.host" in paths
    assert "unifi.tls_fingerprint" in paths
    assert "CIVICRM_API_KEY" in paths
    assert "UNIFI_API_KEY" in paths


# --- env precedence tests ---


def test_env_file_wins_over_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    env.write_text("CIVICRM_API_KEY=from_file\nUNIFI_API_KEY=from_file\n")
    monkeypatch.setenv("CIVICRM_API_KEY", "from_environ")
    monkeypatch.setenv("UNIFI_API_KEY", "from_environ")
    result = load(config_path=cfg, env_path=env)
    assert result.civicrm.api_key == "from_file"


def test_falls_back_to_os_environ_when_file_lacks_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    env.write_text("CIVICRM_API_KEY=from_file\n")  # UNIFI absent
    monkeypatch.setenv("UNIFI_API_KEY", "from_environ")
    result = load(config_path=cfg, env_path=env)
    assert result.unifi.api_key == "from_environ"


def test_missing_required_env_var_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    env.write_text("CIVICRM_API_KEY=civikey\n")
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(i.path == "UNIFI_API_KEY" for i in exc.value.issues)


def test_malformed_env_file_surfaces_as_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    env.write_text("THIS LINE HAS NO EQUALS\n")
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(i.path == "env_file" for i in exc.value.issues)
```

- [ ] **Step 3.5: Run the new tests to confirm they fail (NotImplementedError)**

Run: `uv run pytest tests/test_config.py -v`

Expected: many failures with `NotImplementedError` from `load()` — that's fine. All the validator and load tests will go green once the impl lands in step 3.6.

- [ ] **Step 3.6: Replace `load()` and add validators in `src/door_sync/config.py`**

Replace the `load()` stub (with `raise NotImplementedError`) with the complete implementation, and append the five validator functions. Also add `import re` and `from typing import Any, Callable` to the top import block.

Final import block:
```python
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from door_sync.models import SafetyThresholds, TierMapping, TierRule
```

Module-level constants (place near the top, after imports):
```python
_FINGERPRINT_RE = re.compile(
    r"^([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{64}$"
)

_VALID_RESOLUTIONS = frozenset({"tier", "none", "day-pass"})

EnvGetter = Callable[[str], "str | None"]
```

Replace the `load()` stub with:

```python
def load(
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """Load and validate config from TOML + env. See module docstring for details."""
    config_path, env_path = _resolve_paths(config_path, env_path)
    issues: list[ConfigIssue] = []

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        issues.append(
            ConfigIssue(path="config_file", message=f"file not found: {config_path}")
        )
        raise ConfigError(issues)
    except tomllib.TOMLDecodeError as e:
        issues.append(ConfigIssue(path="config_file", message=f"invalid TOML: {e}"))
        raise ConfigError(issues)

    file_env: dict[str, str] = {}
    try:
        file_env = _load_env_file(env_path)
    except ValueError as e:
        issues.append(ConfigIssue(path="env_file", message=str(e)))

    def env_get(name: str) -> str | None:
        return file_env.get(name) or os.environ.get(name)

    cadence = _validate_cadence(data, issues)
    civicrm = _validate_civicrm(data, issues, env_get)
    unifi = _validate_unifi(data, issues, env_get)
    safety = _validate_safety(data, issues)
    tier_mapping = _validate_tier_mapping(data, issues)

    if issues:
        raise ConfigError(issues)

    return Config(
        cadence_seconds=cadence,
        civicrm=civicrm,
        unifi=unifi,
        safety=safety,
        tier_mapping=tier_mapping,
    )
```

Append the five validators at the end of the file:

```python
def _validate_cadence(data: dict[str, Any], issues: list[ConfigIssue]) -> int:
    value = data.get("cadence_seconds", 600)
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(
            ConfigIssue(
                path="cadence_seconds",
                message=f"must be int, got {type(value).__name__}",
            )
        )
        return 600
    if value < 60:
        issues.append(
            ConfigIssue(
                path="cadence_seconds",
                message=f"must be >= 60, got {value}",
            )
        )
        return 600
    return value


def _validate_civicrm(
    data: dict[str, Any],
    issues: list[ConfigIssue],
    env_get: EnvGetter,
) -> CivicrmConfig:
    section = data.get("civicrm", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="civicrm", message="must be a table"))
        section = {}
    host = section.get("host", "")
    if not isinstance(host, str) or not host:
        issues.append(
            ConfigIssue(path="civicrm.host", message="must be non-empty string")
        )
        host = ""
    elif not host.startswith("https://"):
        issues.append(
            ConfigIssue(
                path="civicrm.host",
                message=f"must start with https://, got {host!r}",
            )
        )
    api_key = (env_get("CIVICRM_API_KEY") or "").strip()
    if not api_key:
        issues.append(
            ConfigIssue(
                path="CIVICRM_API_KEY",
                message="required env var is missing or empty",
            )
        )
    return CivicrmConfig(host=host, api_key=api_key)


def _validate_unifi(
    data: dict[str, Any],
    issues: list[ConfigIssue],
    env_get: EnvGetter,
) -> UnifiConfig:
    section = data.get("unifi", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="unifi", message="must be a table"))
        section = {}
    host = section.get("host", "")
    if not isinstance(host, str) or not host:
        issues.append(
            ConfigIssue(path="unifi.host", message="must be non-empty string")
        )
        host = ""
    elif not host.startswith("https://"):
        issues.append(
            ConfigIssue(
                path="unifi.host",
                message=f"must start with https://, got {host!r}",
            )
        )
    fingerprint = section.get("tls_fingerprint", "")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        issues.append(
            ConfigIssue(
                path="unifi.tls_fingerprint",
                message="must be SHA-256 hex (64 chars or 32 colon-separated bytes)",
            )
        )
        fingerprint = ""
    api_key = (env_get("UNIFI_API_KEY") or "").strip()
    if not api_key:
        issues.append(
            ConfigIssue(
                path="UNIFI_API_KEY",
                message="required env var is missing or empty",
            )
        )
    return UnifiConfig(host=host, api_key=api_key, tls_fingerprint=fingerprint)


def _validate_safety(
    data: dict[str, Any], issues: list[ConfigIssue]
) -> SafetyThresholds:
    section = data.get("safety", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="safety", message="must be a table"))
        section = {}

    def _pct(name: str, default: float) -> float:
        value = section.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(
                ConfigIssue(
                    path=f"safety.{name}",
                    message=f"must be number, got {type(value).__name__}",
                )
            )
            return default
        if not (0 < float(value) <= 1):
            issues.append(
                ConfigIssue(
                    path=f"safety.{name}",
                    message=f"must be between 0 (exclusive) and 1 (inclusive), got {value}",
                )
            )
            return default
        return float(value)

    mass_deactivate = _pct("mass_deactivate_pct", 0.15)
    mass_add = _pct("mass_add_pct", 0.25)
    mass_policy = _pct("mass_policy_pct", 0.20)

    floor_raw = section.get("baseline_floor", 10)
    if isinstance(floor_raw, bool) or not isinstance(floor_raw, int):
        issues.append(
            ConfigIssue(
                path="safety.baseline_floor",
                message=f"must be int, got {type(floor_raw).__name__}",
            )
        )
        floor = 10
    elif floor_raw < 0:
        issues.append(
            ConfigIssue(
                path="safety.baseline_floor",
                message=f"must be >= 0, got {floor_raw}",
            )
        )
        floor = 10
    else:
        floor = floor_raw

    return SafetyThresholds(
        mass_deactivate_pct=mass_deactivate,
        mass_add_pct=mass_add,
        mass_policy_pct=mass_policy,
        baseline_floor=floor,
    )


def _validate_tier_mapping(
    data: dict[str, Any], issues: list[ConfigIssue]
) -> TierMapping:
    section = data.get("tier_mapping", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="tier_mapping", message="must be a table"))
        section = {}
    rules_data = section.get("rules", {})
    if not isinstance(rules_data, dict):
        issues.append(
            ConfigIssue(path="tier_mapping.rules", message="must be a table")
        )
        rules_data = {}

    rules: dict[str, TierRule] = {}
    for name, rule_data in rules_data.items():
        rule_path = f"tier_mapping.rules.{name}"
        if not isinstance(rule_data, dict):
            issues.append(ConfigIssue(path=rule_path, message="must be a table"))
            continue

        resolution = rule_data.get("resolution")
        if resolution not in _VALID_RESOLUTIONS:
            issues.append(
                ConfigIssue(
                    path=f"{rule_path}.resolution",
                    message=f"must be one of tier/none/day-pass, got {resolution!r}",
                )
            )
            continue

        has_target_policy = "target_policy" in rule_data
        target_policy = rule_data.get("target_policy")
        if resolution == "tier":
            if not has_target_policy:
                issues.append(
                    ConfigIssue(
                        path=f"{rule_path}.target_policy",
                        message="required when resolution is 'tier'",
                    )
                )
                continue
            if not isinstance(target_policy, str) or not target_policy:
                issues.append(
                    ConfigIssue(
                        path=f"{rule_path}.target_policy",
                        message="must be non-empty string",
                    )
                )
                continue
        else:
            if has_target_policy:
                issues.append(
                    ConfigIssue(
                        path=f"{rule_path}.target_policy",
                        message=f"must be omitted when resolution is {resolution!r}",
                    )
                )
                continue
            target_policy = None

        rank = rule_data.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int):
            issues.append(
                ConfigIssue(
                    path=f"{rule_path}.rank",
                    message=f"must be int, got {type(rank).__name__}",
                )
            )
            continue

        rules[name] = TierRule(
            resolution=resolution,
            target_policy=target_policy,
            rank=rank,
        )

    return TierMapping(rules=rules)
```

- [ ] **Step 3.7: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: all config tests pass (roughly 50+ new test cases when counting parametrize expansions; total around 120 tests); mypy success; ruff clean.

If `mypy` complains about Any propagation into Literal parameters (e.g., `resolution=resolution`), it should not — mypy accepts `Any` → any concrete type. If it does complain, the fix is a `# type: ignore[arg-type]` on that specific line, but try without first.

If the `host = section.get("host", "")` lines trigger a "Returning Any" warning, suppress with `# type: ignore[no-any-return]` only on the affected return.

- [ ] **Step 3.8: Commit**

```bash
git add src/door_sync/config.py tests/test_config.py
git commit -m "Add config validators and load() with multi-issue collection"
```

---

## Task 4: Example files + docs

**Files:**
- Create: `config.example.toml`
- Create: `.env.example`
- Modify: `README.md` (update the "Schema in progress" line)
- Modify: `tests/test_config.py` (add a drift test that loads the examples)

### Background

Two example files at repo root let a new developer copy + edit + run. A test that calls `load()` on them catches drift between the documented examples and the actual validators.

The `.env.example` ships with stub values; copying it to `.env` and editing the values is the dev setup step.

- [ ] **Step 4.1: Create `config.example.toml` at repo root**

```toml
# door-sync configuration — non-secret settings.
# Copy this file to `config.toml` (dev) or `/etc/door-sync/config.toml` (prod) and edit.
# Secrets (API keys) live in the env file instead — see `.env.example`.

# Polling cadence in seconds. Minimum 60.
cadence_seconds = 600

[civicrm]
# Base URL for the CiviCRM site (must start with https://).
host = "https://civicrm.example.org"

[unifi]
# Base URL for the UniFi Access controller (must start with https://).
host = "https://unifi.example.org:12445"
# SHA-256 fingerprint of the controller's TLS certificate.
# 64 hex chars or 32 colon-separated bytes. Generate with:
#   openssl s_client -connect host:port < /dev/null 2>/dev/null \
#     | openssl x509 -fingerprint -sha256 -noout
tls_fingerprint = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"

[safety]
# These match the defaults baked into SafetyThresholds; tune per deployment.
mass_deactivate_pct = 0.15
mass_add_pct = 0.25
mass_policy_pct = 0.20
baseline_floor = 10

# One [tier_mapping.rules.<TypeName>] table per CiviCRM Membership Type.
# Resolution kinds:
#   "tier"     — assign credential + UniFi policy (target_policy required)
#   "none"     — intentionally no door access (deactivate if present)
#   "day-pass" — skip entirely; day pass flow handles per-visit (Appendix C)
# rank: highest wins when a contact holds multiple types.
# Use quoted keys for type names containing spaces or punctuation.

[tier_mapping.rules.Gold]
resolution = "tier"
target_policy = "policy-id-from-unifi"
rank = 100

[tier_mapping.rules.Comp]
resolution = "none"
rank = 50

[tier_mapping.rules."Day Pass"]
resolution = "day-pass"
rank = 10
```

- [ ] **Step 4.2: Create `.env.example` at repo root**

```
# door-sync secrets.
# Copy this file to `.env` (dev) or `/etc/door-sync/env` (prod, mode 0400) and fill in real values.
# Format matches the simple subset of systemd's EnvironmentFile.

CIVICRM_API_KEY=replace-me
UNIFI_API_KEY=replace-me
```

- [ ] **Step 4.3: Append the drift test to `tests/test_config.py`**

```python
# --- example file drift test ---


def test_example_files_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading the committed example files catches drift between docs and validators."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CIVICRM_API_KEY", raising=False)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    repo_root = Path(__file__).parent.parent
    result = load(
        config_path=repo_root / "config.example.toml",
        env_path=repo_root / ".env.example",
    )
    # The example uses stub values; assert just the shape.
    assert result.civicrm.api_key == "replace-me"
    assert result.unifi.api_key == "replace-me"
    assert result.cadence_seconds == 600
    assert "Gold" in result.tier_mapping.rules
    assert result.tier_mapping.rules["Gold"].resolution == "tier"
    assert "Comp" in result.tier_mapping.rules
    assert result.tier_mapping.rules["Comp"].target_policy is None
```

`Path` and `load` were already imported at the top of `test_config.py` in earlier tasks.

- [ ] **Step 4.4: Update `README.md`**

Find and replace this block in `README.md`:

```markdown
Two files:
 
- **Env file** for secrets (API keys, TLS cert fingerprint). Dev: `.env`. Prod: `/etc/door-sync/env`, mode `0400`.
- **TOML file** for everything else (tier mapping, thresholds, cadence). Dev: `config.toml`. Prod: `/etc/door-sync/config.toml`.
Config schema in progress.
```

With:

```markdown
Two files:
 
- **Env file** for secrets (API keys only). Dev: `.env`. Prod: `/etc/door-sync/env`, mode `0400`.
- **TOML file** for everything else (host URLs, TLS fingerprint, tier mapping, thresholds, cadence). Dev: `config.toml`. Prod: `/etc/door-sync/config.toml`.

See `config.example.toml` and `.env.example` for the full schema. Copy them and fill in real values.
```

(The previous text said the TLS fingerprint goes in env; the implemented schema puts it in TOML. The wording also said "Config schema in progress" — that's no longer true.)

- [ ] **Step 4.5: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: all tests pass including the new drift test; mypy success; ruff clean.

- [ ] **Step 4.6: Commit**

```bash
git add config.example.toml .env.example README.md tests/test_config.py
git commit -m "Add config example files and drift test"
```

---

## Final verification

After Task 4 is committed, do one more pass against the spec's Definition-of-Done (§2):

- [ ] **Step F.1: All three checks green from scratch**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

- [ ] **Step F.2: Every validator from spec §8 has at least one pass and one fail test**

```bash
uv run pytest tests/test_config.py -v --collect-only | grep -E "test_(cadence|civicrm_host|tls_fingerprint|safety_pct|baseline_floor|tier_rule|env_file_)"
```

Expect tests covering: cadence, civicrm.host, unifi.host (via the civicrm host test as a pattern), tls_fingerprint, safety pcts, baseline_floor, tier rule (3 cases), env file (12 cases), required env vars.

- [ ] **Step F.3: Integration test exists and asserts pure-module dataclasses**

```bash
uv run pytest tests/test_config.py::test_load_happy_path_returns_populated_config -v
```

Confirm the test asserts both `Config.safety` is a `SafetyThresholds` (by accessing `.mass_deactivate_pct`) and `Config.tier_mapping.rules["Gold"]` is a `TierRule` (by accessing `.resolution`, `.target_policy`, `.rank`).

- [ ] **Step F.4: Example files parse via the same `load()` consumers will use**

```bash
uv run pytest tests/test_config.py::test_example_files_parse -v
```

- [ ] **Step F.5: `config.py` has no third-party imports**

```bash
grep -nE "^(import|from)" src/door_sync/config.py
```

Expected: only `os`, `re`, `tomllib`, `dataclasses`, `pathlib`, `typing` (all stdlib) and `door_sync.models`. No `pydantic`, no `dotenv`, no `httpx`.

- [ ] **Step F.6: `config.py` never calls `sys.exit`**

```bash
grep -n "sys.exit\|SystemExit" src/door_sync/config.py
```

Expected: no output.

- [ ] **Step F.7: Commit history**

```bash
git log --oneline -6
```

Expected: 4 module commits in order (data shapes, parser, load+validators, examples+docs) plus the earlier spec commit.

If any of F.1–F.7 fails, fix and add a follow-up commit — do not mark the slice done.
