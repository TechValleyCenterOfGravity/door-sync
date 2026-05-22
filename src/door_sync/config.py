"""Config loading and validation for door-sync.

Reads non-secret settings from TOML and secrets from a KEY=value env file.
Returns a frozen Config with the pure-module dataclasses embedded.

This module is not pure (it does file I/O), but it does NOT call sys.exit.
Errors surface as ConfigError so callers can format and exit on their own terms.
"""

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from door_sync.models import SafetyThresholds, TierMapping, TierRule

_FINGERPRINT_RE = re.compile(
    r"^([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{64}$"
)

_VALID_RESOLUTIONS = frozenset({"tier", "none", "day-pass"})

EnvGetter = Callable[[str], "str | None"]


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
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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
            raise ValueError(f"line {line_no}: unmatched quote: {raw_line!r}")
        result[key] = value
    return result


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
            data: dict[str, Any] = tomllib.load(f)
    except FileNotFoundError as exc:
        issues.append(
            ConfigIssue(path="config_file", message=f"file not found: {config_path}")
        )
        raise ConfigError(issues) from exc
    except tomllib.TOMLDecodeError as e:
        issues.append(ConfigIssue(path="config_file", message=f"invalid TOML: {e}"))
        raise ConfigError(issues) from e

    file_env: dict[str, str] = {}
    try:
        file_env = _load_env_file(env_path)
    except ValueError as e:
        issues.append(ConfigIssue(path="env_file", message=str(e)))

    def env_get(name: str) -> str | None:
        # File wins if the key is present at all (even if empty),
        # per spec §7. Only fall through to os.environ when the file
        # genuinely lacks the key.
        val = file_env.get(name)
        return val if val is not None else os.environ.get(name)

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
        host = ""
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
        host = ""
    fingerprint = section.get("tls_fingerprint", "")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        issues.append(
            ConfigIssue(
                path="unifi.tls_fingerprint",
                message=(
                    f"must be SHA-256 hex (64 chars or 32 colon-separated bytes), "
                    f"got {fingerprint!r}"
                ),
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
