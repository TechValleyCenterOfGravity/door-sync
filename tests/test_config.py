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
