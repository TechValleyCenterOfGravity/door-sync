"""Config loading and validation for door-sync.

Reads non-secret settings from TOML and secrets from a KEY=value env file.
Returns a frozen Config with the pure-module dataclasses embedded.

This module is not pure (it does file I/O), but it does NOT call sys.exit.
Errors surface as ConfigError so callers can format and exit on their own terms.
"""

from dataclasses import dataclass
from pathlib import Path

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
