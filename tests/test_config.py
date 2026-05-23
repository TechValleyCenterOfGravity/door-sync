from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import door_sync.config as config_mod
from door_sync.config import (
    _DEFAULT_OPS_PATHS,
    CivicrmConfig,
    Config,
    ConfigError,
    ConfigIssue,
    UnifiConfig,
    _load_env_file,
    load,
)
from door_sync.models import SafetyThresholds, TierMapping


def _minimal_valid_toml() -> str:
    """Smallest valid config.toml — just enough to pass _validate_*."""
    return (
        "cadence_seconds = 600\n"
        "[civicrm]\n"
        'host = "https://civicrm.example.org"\n'
        'card_id_field = "Door_Access.card_id"\n'
        "[unifi]\n"
        'host = "https://unifi.example.org:12445"\n'
        'tls_fingerprint = "'
        + ("AB:" * 31) + 'AB"\n'
        "facility_code = 42\n"
        "[safety]\n"
        "[tier_mapping.rules.Gold]\n"
        'resolution = "tier"\n'
        'target_policy = "p1"\n'
        "rank = 100\n"
    )


def test_civicrm_config_is_frozen() -> None:
    c = CivicrmConfig(host="https://x", api_key="k", card_id_field="G.f")
    with pytest.raises(FrozenInstanceError):
        c.host = "https://y"  # type: ignore[misc]


def test_unifi_config_is_frozen() -> None:
    u = UnifiConfig(host="https://x", api_key="k", tls_fingerprint="AB" * 32, facility_code=0)
    with pytest.raises(FrozenInstanceError):
        u.api_key = "z"  # type: ignore[misc]


def test_config_is_frozen() -> None:
    c = Config(
        cadence_seconds=600,
        civicrm=CivicrmConfig(host="https://x", api_key="k", card_id_field="G.f"),
        unifi=UnifiConfig(host="https://y", api_key="k", tls_fingerprint="AB" * 32, facility_code=0),
        safety=SafetyThresholds(),
        tier_mapping=TierMapping(rules={}),
        ops_paths=_DEFAULT_OPS_PATHS,
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
        '[civicrm]\nhost = "https://c"\ncard_id_field = "G.f"\n'
        '[unifi]\nhost = "https://u"\n'
        'tls_fingerprint = "' + "AB" * 32 + '"\n'
        'facility_code = 42\n'
    )
    env.write_text("CIVICRM_API_KEY=x\nUNIFI_API_KEY=y\n")
    result = load(config_path=cfg, env_path=env)
    assert result.civicrm.host == "https://c"


def test_env_var_dir_supplies_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text(
        'cadence_seconds = 600\n'
        '[civicrm]\nhost = "https://c"\ncard_id_field = "G.f"\n'
        '[unifi]\nhost = "https://u"\n'
        'tls_fingerprint = "' + "AB" * 32 + '"\n'
        'facility_code = 42\n'
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
        'card_id_field = "Door_Access.card_id"\n'
        "[unifi]\n"
        'host = "https://unifi.example.org"\n'
        'tls_fingerprint = "' + ("AB:" * 31 + "AB") + '"\n'
        "facility_code = 42\n"
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
    assert result.civicrm.card_id_field == "Door_Access.card_id"
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


def test_civicrm_missing_card_id_field_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """card_id_field key absent from TOML → ConfigError."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text().replace('card_id_field = "Door_Access.card_id"\n', "")
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "civicrm.card_id_field" for i in exc.value.issues
    )


@pytest.mark.parametrize(
    "bad_value",
    [
        '""',                          # empty string
        '"   "',                       # whitespace-only
        '"Door Access.card_id"',       # internal space
        '"Door_Access\\t.card_id"',    # internal tab
    ],
)
def test_civicrm_card_id_field_rejects_bad_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_value: str,
) -> None:
    """card_id_field must be non-empty and contain no internal whitespace."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text().replace(
            '"Door_Access.card_id"',
            bad_value,
        )
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "civicrm.card_id_field" for i in exc.value.issues
    )


def test_civicrm_card_id_field_strips_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leading/trailing whitespace is stripped before storage."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text().replace(
            '"Door_Access.card_id"',
            '"  Door_Access.card_id  "',
        )
    )
    result = load(config_path=cfg, env_path=env)
    assert result.civicrm.card_id_field == "Door_Access.card_id"


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


def test_tier_rule_invalid_rank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rank must be an int. Strings, bools, and missing values are all rejected."""
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    cfg, env = _write_minimal_valid(tmp_path)
    cfg.write_text(
        cfg.read_text()
        + '[tier_mapping.rules.Bad]\nresolution = "none"\nrank = "oops"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "tier_mapping.rules.Bad.rank" for i in exc.value.issues
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
    assert "unifi.facility_code" in paths


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
    """Loading the committed example files catches drift between docs and validators.

    Asserts every field the validators populate, so a schema change in any
    section breaks this test if the example files aren't updated in lockstep.
    """
    monkeypatch.delenv("DOOR_SYNC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CIVICRM_API_KEY", raising=False)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    repo_root = Path(__file__).parent.parent
    result = load(
        config_path=repo_root / "config.example.toml",
        env_path=repo_root / ".env.example",
    )
    # Top-level
    assert result.cadence_seconds == 600
    # civicrm
    assert result.civicrm.host == "https://civicrm.example.org"
    assert result.civicrm.api_key == "replace-me"
    assert result.civicrm.card_id_field == "Door_Access.card_id"
    # unifi
    assert result.unifi.host == "https://unifi.example.org:12445"
    assert result.unifi.api_key == "replace-me"
    assert result.unifi.tls_fingerprint.startswith("AB:CD:EF:")
    assert result.unifi.facility_code == 42
    # safety (verifies _validate_safety builds the dataclass with example values)
    assert isinstance(result.safety, SafetyThresholds)
    assert result.safety.mass_deactivate_pct == 0.15
    assert result.safety.mass_add_pct == 0.25
    assert result.safety.mass_policy_pct == 0.20
    assert result.safety.baseline_floor == 10
    # tier_mapping — all three rules from the example
    assert "Gold" in result.tier_mapping.rules
    assert result.tier_mapping.rules["Gold"].resolution == "tier"
    assert result.tier_mapping.rules["Gold"].target_policy == "policy-id-from-unifi"
    assert result.tier_mapping.rules["Gold"].rank == 100
    assert "Comp" in result.tier_mapping.rules
    assert result.tier_mapping.rules["Comp"].resolution == "none"
    assert result.tier_mapping.rules["Comp"].target_policy is None
    # The "Day Pass" quoted-key rule — most likely to break on parser changes
    assert "Day Pass" in result.tier_mapping.rules
    assert result.tier_mapping.rules["Day Pass"].resolution == "day-pass"
    assert result.tier_mapping.rules["Day Pass"].target_policy is None


# --- facility_code tests ---


def test_load_rejects_missing_facility_code(tmp_path: Path) -> None:
    """facility_code is required; absence is a clear ConfigError."""
    cfg, env = _write_minimal_valid(tmp_path)
    # Strip the facility_code line we just added in the helper.
    content = cfg.read_text()
    content = "\n".join(
        line for line in content.splitlines()
        if not line.strip().startswith("facility_code")
    )
    cfg.write_text(content)
    with pytest.raises(ConfigError) as exc_info:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "unifi.facility_code"
        for i in exc_info.value.issues
    )


@pytest.mark.parametrize(
    "value,reason",
    [
        ("-1", "must be between 0 and 255"),
        ("256", "must be between 0 and 255"),
        ('"forty-two"', "must be int"),
        ("true", "must be int"),
    ],
)
def test_load_rejects_invalid_facility_code(
    tmp_path: Path, value: str, reason: str
) -> None:
    """Out-of-range or wrong-type facility_code raises with helpful message."""
    cfg, env = _write_minimal_valid(tmp_path)
    content = cfg.read_text()
    # Replace the facility_code = 42 line.
    content = "\n".join(
        f"facility_code = {value}" if line.strip().startswith("facility_code")
        else line
        for line in content.splitlines()
    )
    cfg.write_text(content)
    with pytest.raises(ConfigError) as exc_info:
        load(config_path=cfg, env_path=env)
    assert any(
        i.path == "unifi.facility_code" and reason in i.message
        for i in exc_info.value.issues
    ), [i for i in exc_info.value.issues]


@pytest.mark.parametrize("code", [0, 255])
def test_load_accepts_facility_code_boundary_values(
    tmp_path: Path, code: int
) -> None:
    """Range check is inclusive on both ends: 0 and 255 are valid."""
    config_path, env_path = _write_minimal_valid(tmp_path)
    content = config_path.read_text()
    content = content.replace("facility_code = 42", f"facility_code = {code}")
    config_path.write_text(content)
    result = load(config_path=config_path, env_path=env_path)
    assert result.unifi.facility_code == code


# --- ops_paths tests ---


def test_ops_paths_default_when_section_omitted(tmp_path: Path) -> None:
    """If [ops] is missing entirely, defaults from architecture §11 apply."""
    cfg_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    cfg_path.write_text(_minimal_valid_toml(), encoding="utf-8")
    env_path.write_text("CIVICRM_API_KEY=k\nUNIFI_API_KEY=k\n", encoding="utf-8")

    config = config_mod.load(config_path=cfg_path, env_path=env_path)

    assert config.ops_paths.audit_jsonl == Path("/var/log/door-sync/audit.jsonl")
    assert config.ops_paths.state_json == Path("/var/lib/door-sync/state.json")
    assert config.ops_paths.alert_flag == Path("/var/run/door-sync/alert.flag")


def test_ops_paths_explicit_values_override_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    cfg_path.write_text(
        _minimal_valid_toml() + (
            "\n[ops]\n"
            'audit_jsonl = "/tmp/a.jsonl"\n'
            'state_json  = "/tmp/s.json"\n'
            'alert_flag  = "/tmp/f.flag"\n'
        ),
        encoding="utf-8",
    )
    env_path.write_text("CIVICRM_API_KEY=k\nUNIFI_API_KEY=k\n", encoding="utf-8")

    config = config_mod.load(config_path=cfg_path, env_path=env_path)

    assert config.ops_paths.audit_jsonl == Path("/tmp/a.jsonl")
    assert config.ops_paths.state_json == Path("/tmp/s.json")
    assert config.ops_paths.alert_flag == Path("/tmp/f.flag")


def test_ops_paths_rejects_non_string_value(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    cfg_path.write_text(
        _minimal_valid_toml() + (
            "\n[ops]\n"
            "audit_jsonl = 42\n"
        ),
        encoding="utf-8",
    )
    env_path.write_text("CIVICRM_API_KEY=k\nUNIFI_API_KEY=k\n", encoding="utf-8")

    with pytest.raises(config_mod.ConfigError) as excinfo:
        config_mod.load(config_path=cfg_path, env_path=env_path)

    paths = [issue.path for issue in excinfo.value.issues]
    assert "ops.audit_jsonl" in paths
