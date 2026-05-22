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
