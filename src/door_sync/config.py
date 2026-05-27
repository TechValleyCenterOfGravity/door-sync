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
from typing import Any, Literal, cast

from door_sync.models import SafetyThresholds, TierMapping, TierRule

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_FINGERPRINT_RE = re.compile(r"^(?:(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{64})$")

_VALID_RESOLUTIONS = frozenset({"tier", "none", "day-pass"})

EnvGetter = Callable[[str], "str | None"]


@dataclass(frozen=True)
class CivicrmConfig:
    """CiviCRM API4 connection settings. Secret: api_key (from env).

    Parameters:
        host: Base URL of the WordPress/CiviCRM instance (must be https).
        api_key: Bearer token for CiviCRM API4 authentication.
        card_id_field: CiviCRM custom field name that holds the card ID.
        active_statuses: Membership status names that grant door access.
    """

    host: str
    api_key: str
    card_id_field: str
    active_statuses: tuple[str, ...]


@dataclass(frozen=True)
class UnifiConfig:
    """UniFi Access controller connection settings. Secret: api_key (from env).

    Parameters:
        host: Base URL of the UniFi Access controller (must be https).
        api_key: Bearer token for UniFi local API authentication.
        tls_fingerprint: SHA-256 fingerprint of the controller's TLS certificate.
        facility_code: Wiegand-26 facility code (0-255) for NFC card encoding.
    """

    host: str
    api_key: str
    tls_fingerprint: str
    facility_code: int


@dataclass(frozen=True)
class SmtpConfig:
    """SMTP alert transport settings. Secrets: username, password (from env).

    Parameters:
        host: SMTP server hostname.
        port: SMTP server port.
        starttls: Whether to upgrade the connection with STARTTLS.
        username: SMTP authentication username.
        password: SMTP authentication password.
        from_addr: Sender email address.
        to_addrs: Recipient email addresses.
        subject_prefix: String prepended to all alert email subjects.
    """

    host: str
    port: int
    starttls: bool
    username: str
    password: str
    from_addr: str
    to_addrs: tuple[str, ...]
    subject_prefix: str


@dataclass(frozen=True)
class MailgunConfig:
    """Mailgun HTTP API alert transport settings. Secret: api_key (from env).

    Parameters:
        domain: Mailgun sending domain.
        api_key: Mailgun API key for authentication.
        from_addr: Sender email address.
        to_addrs: Recipient email addresses.
        subject_prefix: String prepended to all alert email subjects.
    """

    domain: str
    api_key: str
    from_addr: str
    to_addrs: tuple[str, ...]
    subject_prefix: str


@dataclass(frozen=True)
class AlertConfig:
    """Alert transport selector. smtp/mailgun populated only when transport matches.

    Parameters:
        transport: Active transport: flag-file, smtp, or mailgun.
        smtp: SMTP settings when transport is 'smtp', None otherwise.
        mailgun: Mailgun settings when transport is 'mailgun', None otherwise.
    """

    transport: Literal["flag-file", "smtp", "mailgun"]
    smtp: SmtpConfig | None
    mailgun: MailgunConfig | None


_DEFAULT_ALERT_CONFIG = AlertConfig(transport="flag-file", smtp=None, mailgun=None)


@dataclass(frozen=True)
class OpsPaths:
    """File paths for operational artifacts (audit log, state, alert flag).

    Parameters:
        audit_jsonl: Path to the append-only JSONL audit log.
        state_json: Path to the persistent state JSON file.
        alert_flag: Path to the alert flag file.
    """

    audit_jsonl: Path
    state_json: Path
    alert_flag: Path


@dataclass(frozen=True)
class Config:
    """Top-level configuration assembled from TOML + env by `load()`.

    Parameters:
        cadence_seconds: Seconds between reconciliation cycles in daemon mode.
        civicrm: CiviCRM API connection settings.
        unifi: UniFi Access controller connection settings.
        safety: Thresholds for mass-change safety guards.
        tier_mapping: Rules mapping membership types to access policies.
        ops_paths: File paths for audit log, state, and alert flag.
        alert: Alert transport configuration.
    """

    cadence_seconds: int
    civicrm: CivicrmConfig
    unifi: UnifiConfig
    safety: SafetyThresholds
    tier_mapping: TierMapping
    ops_paths: OpsPaths
    alert: AlertConfig


@dataclass(frozen=True)
class ConfigIssue:
    """Single validation error: dotted path to the offending key + message.

    Parameters:
        path: Dotted config key path (e.g. 'civicrm.host') or env var name.
        message: Human-readable description of the validation failure.
    """

    path: str
    message: str


class ConfigError(Exception):
    """Raised when configuration loading or validation produces one or more issues."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        """Initialize with one or more validation issues.

        Args:
            issues: List of config validation errors.
        """
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
            raise ValueError(f"line {line_no}: not KEY=value, comment, or blank: {raw_line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"line {line_no}: empty key: {raw_line!r}")
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        elif value and (value[0] in ('"', "'") or value[-1] in ('"', "'")):
            raise ValueError(f"line {line_no}: unmatched quote: {raw_line!r}")
        result[key] = value
    return result


def _resolve_paths(config_path: Path | None, env_path: Path | None) -> tuple[Path, Path]:
    config_dir = os.environ.get("DOOR_SYNC_CONFIG_DIR")
    if config_path is None:
        config_path = Path(config_dir) / "config.toml" if config_dir else Path("config.toml")
    if env_path is None:
        env_path = Path(config_dir) / "env" if config_dir else Path(".env")
    return config_path, env_path


def load(
    *,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """Load and validate configuration from TOML and env files.

    Args:
        config_path: Path to config.toml. Defaults to
            $DOOR_SYNC_CONFIG_DIR/config.toml or ./config.toml.
        env_path: Path to env file. Defaults to
            $DOOR_SYNC_CONFIG_DIR/env or ./.env.

    Returns:
        Fully validated `Config` instance.

    Raises:
        ConfigError: If the config file is missing, malformed, or contains invalid values.
    """
    config_path, env_path = _resolve_paths(config_path, env_path)
    issues: list[ConfigIssue] = []

    try:
        with config_path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
    except FileNotFoundError as exc:
        issues.append(ConfigIssue(path="config_file", message=f"file not found: {config_path}"))
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
    ops_paths = _validate_ops(data, issues)
    alert_config = _validate_alert(data, issues, env_get)

    if issues:
        raise ConfigError(issues)

    return Config(
        cadence_seconds=cadence,
        civicrm=civicrm,
        unifi=unifi,
        safety=safety,
        tier_mapping=tier_mapping,
        ops_paths=ops_paths,
        alert=alert_config,
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
        issues.append(ConfigIssue(path="civicrm.host", message="must be non-empty string"))
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
    card_id_field_raw = section.get("card_id_field", "")
    if not isinstance(card_id_field_raw, str):
        issues.append(
            ConfigIssue(
                path="civicrm.card_id_field",
                message="must be non-empty string",
            )
        )
        card_id_field = ""
    else:
        card_id_field = card_id_field_raw.strip()
        if not card_id_field:
            issues.append(
                ConfigIssue(
                    path="civicrm.card_id_field",
                    message="must be non-empty string",
                )
            )
        elif any(c.isspace() for c in card_id_field):
            issues.append(
                ConfigIssue(
                    path="civicrm.card_id_field",
                    message="must not contain internal whitespace",
                )
            )
            card_id_field = ""
    active_statuses_raw = section.get("active_statuses", ["Current", "Grace", "New"])
    if not isinstance(active_statuses_raw, list) or not all(
        isinstance(s, str) for s in active_statuses_raw
    ):
        issues.append(
            ConfigIssue(
                path="civicrm.active_statuses",
                message="must be a list of strings",
            )
        )
        active_statuses: tuple[str, ...] = ("Current", "Grace", "New")
    else:
        stripped = [s.strip() for s in active_statuses_raw]
        empty = [s for s in stripped if not s]
        if empty:
            issues.append(
                ConfigIssue(
                    path="civicrm.active_statuses",
                    message="entries must be non-empty strings",
                )
            )
        active_statuses = tuple(stripped) if not empty else ("Current", "Grace", "New")
    return CivicrmConfig(
        host=host,
        api_key=api_key,
        card_id_field=card_id_field,
        active_statuses=active_statuses,
    )


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
        issues.append(ConfigIssue(path="unifi.host", message="must be non-empty string"))
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
    facility_code_raw = section.get("facility_code")
    if facility_code_raw is None:
        issues.append(
            ConfigIssue(
                path="unifi.facility_code",
                message="required: Wiegand-26 facility code (0-255)",
            )
        )
        facility_code = 0
    elif isinstance(facility_code_raw, bool) or not isinstance(facility_code_raw, int):
        issues.append(
            ConfigIssue(
                path="unifi.facility_code",
                message=f"must be int, got {type(facility_code_raw).__name__}",
            )
        )
        facility_code = 0
    elif not (0 <= facility_code_raw <= 255):
        issues.append(
            ConfigIssue(
                path="unifi.facility_code",
                message=f"must be between 0 and 255, got {facility_code_raw}",
            )
        )
        facility_code = 0
    else:
        facility_code = facility_code_raw
    return UnifiConfig(
        host=host,
        api_key=api_key,
        tls_fingerprint=fingerprint,
        facility_code=facility_code,
    )


def _validate_safety(data: dict[str, Any], issues: list[ConfigIssue]) -> SafetyThresholds:
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


_DEFAULT_OPS_PATHS = OpsPaths(
    audit_jsonl=Path("/var/log/door-sync/audit.jsonl"),
    state_json=Path("/var/lib/door-sync/state.json"),
    alert_flag=Path("/var/run/door-sync/alert.flag"),
)


def _validate_ops(data: dict[str, Any], issues: list[ConfigIssue]) -> OpsPaths:
    section = data.get("ops", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="ops", message="must be a table"))
        return _DEFAULT_OPS_PATHS

    def _string_path(key: str, default: Path) -> Path:
        raw = section.get(key)
        if raw is None:
            return default
        if not isinstance(raw, str):
            issues.append(
                ConfigIssue(
                    path=f"ops.{key}",
                    message=f"must be string, got {type(raw).__name__}",
                )
            )
            return default
        return Path(raw)

    return OpsPaths(
        audit_jsonl=_string_path("audit_jsonl", _DEFAULT_OPS_PATHS.audit_jsonl),
        state_json=_string_path("state_json", _DEFAULT_OPS_PATHS.state_json),
        alert_flag=_string_path("alert_flag", _DEFAULT_OPS_PATHS.alert_flag),
    )


_VALID_TRANSPORTS = frozenset({"flag-file", "smtp", "mailgun"})


def _validate_alert(
    data: dict[str, Any],
    issues: list[ConfigIssue],
    env_get: EnvGetter,
) -> AlertConfig:
    section = data.get("alert", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="alert", message="must be a table"))
        return _DEFAULT_ALERT_CONFIG

    transport = section.get("transport", "flag-file")
    if transport not in _VALID_TRANSPORTS:
        issues.append(
            ConfigIssue(
                path="alert.transport",
                message=f"must be one of flag-file/smtp/mailgun, got {transport!r}",
            )
        )
        return _DEFAULT_ALERT_CONFIG

    smtp: SmtpConfig | None = None
    mailgun: MailgunConfig | None = None

    if transport == "smtp":
        smtp = _validate_smtp(section, issues, env_get)
    elif transport == "mailgun":
        mailgun = _validate_mailgun(section, issues, env_get)

    return AlertConfig(
        transport=cast(Literal["flag-file", "smtp", "mailgun"], transport),
        smtp=smtp,
        mailgun=mailgun,
    )


def _validate_email_addrs(
    raw: Any,
    path: str,
    issues: list[ConfigIssue],
) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        issues.append(ConfigIssue(path=path, message="must be a non-empty list of email addresses"))
        return ()
    addrs: list[str] = []
    for i, addr in enumerate(raw):
        if not isinstance(addr, str) or not _EMAIL_RE.match(addr):
            issues.append(
                ConfigIssue(
                    path=f"{path}[{i}]",
                    message=f"invalid email address: {addr!r}",
                )
            )
        else:
            addrs.append(addr)
    return tuple(addrs)


def _validate_smtp(
    section: dict[str, Any],
    issues: list[ConfigIssue],
    env_get: EnvGetter,
) -> SmtpConfig:
    smtp = section.get("smtp", {})
    if not isinstance(smtp, dict):
        issues.append(ConfigIssue(path="alert.smtp", message="must be a table"))
        smtp = {}

    host = smtp.get("host", "")
    if not isinstance(host, str) or not host:
        issues.append(ConfigIssue(path="alert.smtp.host", message="must be non-empty string"))
        host = ""

    port = smtp.get("port", 587)
    if isinstance(port, bool) or not isinstance(port, int):
        issues.append(
            ConfigIssue(
                path="alert.smtp.port",
                message=f"must be int, got {type(port).__name__}",
            )
        )
        port = 587
    elif not (1 <= port <= 65535):
        issues.append(
            ConfigIssue(
                path="alert.smtp.port",
                message=f"must be between 1 and 65535, got {port}",
            )
        )
        port = 587

    starttls = smtp.get("starttls", True)
    if not isinstance(starttls, bool):
        issues.append(
            ConfigIssue(
                path="alert.smtp.starttls",
                message=f"must be bool, got {type(starttls).__name__}",
            )
        )
        starttls = True

    from_addr = smtp.get("from", "")
    if not isinstance(from_addr, str) or not _EMAIL_RE.match(from_addr):
        issues.append(
            ConfigIssue(
                path="alert.smtp.from",
                message=f"must be a valid email address, got {from_addr!r}",
            )
        )
        from_addr = ""

    to_addrs = _validate_email_addrs(smtp.get("to"), "alert.smtp.to", issues)

    subject_prefix = smtp.get("subject_prefix", "[door-sync]")
    if not isinstance(subject_prefix, str):
        issues.append(
            ConfigIssue(
                path="alert.smtp.subject_prefix",
                message=f"must be string, got {type(subject_prefix).__name__}",
            )
        )
        subject_prefix = "[door-sync]"

    username = (env_get("SMTP_USERNAME") or "").strip()
    if not username:
        issues.append(
            ConfigIssue(
                path="SMTP_USERNAME",
                message="required env var is missing or empty when alert.transport is 'smtp'",
            )
        )
    password = (env_get("SMTP_PASSWORD") or "").strip()
    if not password:
        issues.append(
            ConfigIssue(
                path="SMTP_PASSWORD",
                message="required env var is missing or empty when alert.transport is 'smtp'",
            )
        )

    return SmtpConfig(
        host=host,
        port=port,
        starttls=starttls,
        username=username,
        password=password,
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject_prefix=subject_prefix,
    )


def _validate_mailgun(
    section: dict[str, Any],
    issues: list[ConfigIssue],
    env_get: EnvGetter,
) -> MailgunConfig:
    mg = section.get("mailgun", {})
    if not isinstance(mg, dict):
        issues.append(ConfigIssue(path="alert.mailgun", message="must be a table"))
        mg = {}

    domain = mg.get("domain", "")
    if not isinstance(domain, str) or not domain:
        issues.append(
            ConfigIssue(
                path="alert.mailgun.domain",
                message="must be non-empty string",
            )
        )
        domain = ""

    from_addr = mg.get("from", "")
    if not isinstance(from_addr, str) or not _EMAIL_RE.match(from_addr):
        issues.append(
            ConfigIssue(
                path="alert.mailgun.from",
                message=f"must be a valid email address, got {from_addr!r}",
            )
        )
        from_addr = ""

    to_addrs = _validate_email_addrs(mg.get("to"), "alert.mailgun.to", issues)

    subject_prefix = mg.get("subject_prefix", "[door-sync]")
    if not isinstance(subject_prefix, str):
        issues.append(
            ConfigIssue(
                path="alert.mailgun.subject_prefix",
                message=f"must be string, got {type(subject_prefix).__name__}",
            )
        )
        subject_prefix = "[door-sync]"

    api_key = (env_get("MAILGUN_API_KEY") or "").strip()
    if not api_key:
        issues.append(
            ConfigIssue(
                path="MAILGUN_API_KEY",
                message="required env var is missing or empty when alert.transport is 'mailgun'",
            )
        )

    return MailgunConfig(
        domain=domain,
        api_key=api_key,
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject_prefix=subject_prefix,
    )


def _validate_tier_mapping(data: dict[str, Any], issues: list[ConfigIssue]) -> TierMapping:
    section = data.get("tier_mapping", {})
    if not isinstance(section, dict):
        issues.append(ConfigIssue(path="tier_mapping", message="must be a table"))
        section = {}
    rules_data = section.get("rules", {})
    if not isinstance(rules_data, dict):
        issues.append(ConfigIssue(path="tier_mapping.rules", message="must be a table"))
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
            resolution=cast(Literal["tier", "none", "day-pass"], resolution),
            target_policy=target_policy,
            rank=rank,
        )

    return TierMapping(rules=rules)
