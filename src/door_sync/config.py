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
