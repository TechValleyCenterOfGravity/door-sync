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
    load,
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


def test_env_file_trailing_orphan_quote_raises(tmp_path: Path) -> None:
    p = tmp_path / "env"
    p.write_text('KEY=value"\n')
    with pytest.raises(ValueError, match="unmatched quote"):
        _load_env_file(p)


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


def test_empty_env_file_value_does_not_fall_through_to_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty value in the .env file is treated as 'intentionally empty', not 'missing'.

    File wins per spec §7. A developer who writes `KEY=` in .env to suppress a
    shell variable expects that to work — the env var should NOT leak through.
    """
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    env.write_text("CIVICRM_API_KEY=\nUNIFI_API_KEY=unifikey\n")
    monkeypatch.setenv("CIVICRM_API_KEY", "from_environ")
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(i.path == "CIVICRM_API_KEY" for i in exc.value.issues)


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
