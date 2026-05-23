# UniFi Access Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `src/door_sync/unifi/client.py` so the orchestrator can call `UnifiClient(config.unifi, dry_run=...).fetch_users() -> list[UnifiUser]` and `UnifiClient(...).apply(diff)` to read and write against a real UniFi Access controller (≥ v3.3.10) over its self-signed-cert local API.

**Architecture:** Sync `httpx` client; per-cycle instance with a TLS fingerprint pinned at construction. Reads paginate user and NFC-card endpoints; writes import unknown cards via the documented 2-column CSV upload then bind tokens to users. Hand-rolled retries with exponential backoff, `Retry-After` honored, response envelope unwrapped centrally. Tests use `pytest-httpx` (already a dev dep from the CiviCRM slice).

**Tech Stack:** Python 3.11+, `uv`, sync `httpx` (existing dep). Stdlib `socket`, `ssl`, `hashlib`, `csv`, `io`, `logging`, `time`, `random`. No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-05-22-unifi-client-design.md`](../specs/2026-05-22-unifi-client-design.md).

**Conventions (architecture §11):**
- Type hints on every function. `mypy --strict src tests` must be green.
- Imports: stdlib → third-party → `door_sync.*`. No `from x import *`.
- No `sys.exit`. Errors raise `UnifiClientError`.
- No `assert` for invariants — use explicit `if`.
- Card IDs are sensitive: every log statement that references a card_id passes it through `_redact()`. Never log a full card_id at any level.

**Verification commands** (used at the end of every task):

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

**Pattern to mirror:** [`src/door_sync/civicrm/client.py`](../../../src/door_sync/civicrm/client.py) sets the precedent for retry helper shape, error envelope, context manager, and test patterns. When in doubt, match that file's style.

**API verification status:** The spec was end-to-end verified against the real controller during brainstorming (auth header, base URL, response envelope, card-ID encoding `(FC << 16) | CN` as hex, 2-column CSV API format, multipart shape, import-then-unlock flow). No additional manual verification is needed before kicking off Task 1. The tests below pin every assumption.

---

## Task 1: Add `facility_code` to `UnifiConfig`

**Files:**
- Modify: `src/door_sync/config.py` (extend `UnifiConfig` dataclass + `_validate_unifi`)
- Modify: `config.example.toml` (document new field)
- Modify: `tests/test_config.py` (update helper, existing tests, add new tests)

### Background

The UniFi client needs the Wiegand-26 facility code to encode card_ids into the `nfc_id` format UniFi uses. The value is site-specific (set by the access-control vendor when cards were programmed). Adding it to config now means the client constructor can read it directly.

- [ ] **Step 1.1: Update `UnifiConfig` in `src/door_sync/config.py`**

Find the existing class:
```python
@dataclass(frozen=True)
class UnifiConfig:
    host: str
    api_key: str
    tls_fingerprint: str
```

Replace with:
```python
@dataclass(frozen=True)
class UnifiConfig:
    host: str
    api_key: str
    tls_fingerprint: str
    facility_code: int
```

- [ ] **Step 1.2: Extend `_validate_unifi` to require `facility_code`**

Find this block at the end of `_validate_unifi` in `src/door_sync/config.py`:

```python
    api_key = (env_get("UNIFI_API_KEY") or "").strip()
    if not api_key:
        issues.append(
            ConfigIssue(
                path="UNIFI_API_KEY",
                message="required env var is missing or empty",
            )
        )
    return UnifiConfig(host=host, api_key=api_key, tls_fingerprint=fingerprint)
```

Replace with:

```python
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
```

- [ ] **Step 1.3: Update `config.example.toml`**

Find the existing `[unifi]` block and add the facility_code line at the bottom:

```toml
# Wiegand 26-bit facility code (0-255), constant per site.
# Get this from your access-control vendor or by reading any existing
# enrolled card via the UniFi Access UI > Credentials > NFC Cards
# (the value is encoded in nfc_id as the upper byte of the hex).
facility_code = 42
```

- [ ] **Step 1.4: Update the `_write_minimal_valid` helper in `tests/test_config.py`**

Find the existing helper that writes a minimal valid config TOML. Add `facility_code = 42` inside the `[unifi]` table so existing happy-path tests still pass.

If the helper uses a single multi-line string template, find the `[unifi]` table within it (look for `tls_fingerprint = ` line) and add `facility_code = 42` on the line after it.

- [ ] **Step 1.5: Write failing test for missing facility_code**

Add to `tests/test_config.py`:

```python
def test_load_rejects_missing_facility_code(tmp_path: Path) -> None:
    """facility_code is required; absence is a clear ConfigError."""
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    _write_minimal_valid(config_path, env_path)
    # Strip the facility_code line we just added in the helper.
    content = config_path.read_text()
    content = "\n".join(
        line for line in content.splitlines()
        if not line.strip().startswith("facility_code")
    )
    config_path.write_text(content)
    with pytest.raises(ConfigError) as exc_info:
        load(config_path=config_path, env_path=env_path)
    assert any(
        i.path == "unifi.facility_code"
        for i in exc_info.value.issues
    )
```

- [ ] **Step 1.6: Run test, confirm it fails**

Run: `uv run pytest tests/test_config.py::test_load_rejects_missing_facility_code -v`
Expected: PASS (because Step 1.2 already added the validation). If it doesn't pass, the validation logic in Step 1.2 is wrong — fix before continuing.

- [ ] **Step 1.7: Add tests for invalid facility_code values**

Add to `tests/test_config.py`:

```python
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
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / "env"
    _write_minimal_valid(config_path, env_path)
    content = config_path.read_text()
    # Replace the facility_code = 42 line.
    content = "\n".join(
        f"facility_code = {value}" if line.strip().startswith("facility_code")
        else line
        for line in content.splitlines()
    )
    config_path.write_text(content)
    with pytest.raises(ConfigError) as exc_info:
        load(config_path=config_path, env_path=env_path)
    assert any(
        i.path == "unifi.facility_code" and reason in i.message
        for i in exc_info.value.issues
    ), [i for i in exc_info.value.issues]
```

- [ ] **Step 1.8: Run new tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: ALL PASS, including any previously-existing tests (which now have `facility_code = 42` in the helper-written TOML).

- [ ] **Step 1.9: Add a drift test for `config.example.toml`**

Find the existing drift test in `tests/test_config.py` (looks like `test_example_config_drift` or similar). It should already validate that loading `config.example.toml` produces a working `Config`. The drift test will catch if you forgot Step 1.3 — confirm it still passes.

If there's no existing drift test, add one:

```python
def test_example_config_loads_with_minimal_env(tmp_path: Path) -> None:
    """config.example.toml at repo root must be loadable with the env vars it documents."""
    repo_root = Path(__file__).parent.parent
    env_path = tmp_path / "env"
    env_path.write_text(
        "CIVICRM_API_KEY=test\nUNIFI_API_KEY=test\n"
    )
    config = load(config_path=repo_root / "config.example.toml", env_path=env_path)
    assert config.unifi.facility_code == 42
```

- [ ] **Step 1.10: Run all config tests + mypy + ruff**

```bash
uv run pytest tests/test_config.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 1.11: Commit**

```bash
git add src/door_sync/config.py config.example.toml tests/test_config.py
git commit -m "Add facility_code field to UnifiConfig"
```

---

## Task 2: Card-ID encoding helpers

**Files:**
- Create: `src/door_sync/unifi/__init__.py`
- Create: `src/door_sync/unifi/client.py`
- Create: `tests/test_unifi_client.py`

### Background

Spec §10 verifies that UniFi encodes Wiegand-26 cards as `nfc_id = uppercase hex of (FC << 16) | CN`. We need pure functions to compute and parse this. These are the smallest, most testable units — implement first.

- [ ] **Step 2.1: Create the package marker**

Create `src/door_sync/unifi/__init__.py` as an empty file (matches `src/door_sync/civicrm/__init__.py`).

- [ ] **Step 2.2: Create the client.py skeleton with the two helpers**

Create `src/door_sync/unifi/client.py`:

```python
"""UniFi Access local-API client for door-sync.

Reads users (sync-managed users have employee_number set to their CiviCRM
contact_id) and applies a Diff: deactivates departed members, updates
credentials and policies, registers and binds new NFC cards.

This module is not pure (HTTP, TLS, optional logging in dry-run). Errors
surface as UnifiClientError; the scheduler's per-cycle try/except handles
them. See docs/architecture.md §4-§5 for the layering rules.
"""


def _compute_nfc_id(facility_code: int, card_id: int) -> str:
    """Encode a Wiegand-26 (FC, CN) pair the way UniFi exposes it in nfc_id.

    Uppercase hex of (FC << 16) | CN, no zero-padding.
    Example: (42, 1234) -> "2A04D2", (42, 1235) -> "2A04D3".
    """
    return f"{(facility_code << 16) | card_id:X}"


def _parse_nfc_id(nfc_id: str, expected_facility_code: int) -> int | None:
    """Decode UniFi's nfc_id back to a Wiegand card_number (CN).

    Returns the CN if the encoded facility code matches expected_facility_code.
    Returns None on parse failure or FC mismatch — the latter is treated as
    "card not in our namespace" and surfaces as a warning at the call site.
    """
    try:
        value = int(nfc_id, 16)
    except ValueError:
        return None
    fc = (value >> 16) & 0xFF
    cn = value & 0xFFFF
    if fc != expected_facility_code:
        return None
    return cn
```

- [ ] **Step 2.3: Create `tests/test_unifi_client.py` with the helper tests**

Create `tests/test_unifi_client.py`:

```python
"""Tests for the UniFi Access client."""

from door_sync.unifi.client import _compute_nfc_id, _parse_nfc_id


# --- Card-ID encoding helpers ---


def test_compute_nfc_id_known_values() -> None:
    """Verified encoding: (FC << 16) | CN as uppercase hex, no padding."""
    assert _compute_nfc_id(42, 1234) == "2A04D2"
    assert _compute_nfc_id(42, 1235) == "2A04D3"


def test_compute_nfc_id_zero_card_number() -> None:
    """CN=0 still produces FC-prefixed hex, not just '0'."""
    assert _compute_nfc_id(42, 0) == "2A0000"


def test_parse_nfc_id_matching_facility_code() -> None:
    """Inverse of _compute_nfc_id for the same FC."""
    assert _parse_nfc_id("2A04D2", 42) == 1234
    assert _parse_nfc_id("2A04D3", 42) == 1235


def test_parse_nfc_id_mismatched_facility_code_returns_none() -> None:
    """Foreign-FC cards are out of our namespace; signal via None."""
    assert _parse_nfc_id("2A04D2", 99) is None


def test_parse_nfc_id_garbage_returns_none() -> None:
    """Unparseable strings return None instead of raising."""
    assert _parse_nfc_id("not-hex", 42) is None
    assert _parse_nfc_id("", 42) is None


def test_parse_nfc_id_lowercase_hex_still_parses() -> None:
    """Defensive: don't trust UniFi to always uppercase the response."""
    assert _parse_nfc_id("2a04d3", 42) == 1235
```

- [ ] **Step 2.4: Run the tests, confirm they pass**

```bash
uv run pytest tests/test_unifi_client.py -v
```

Expected: ALL 6 tests PASS.

- [ ] **Step 2.5: Run mypy + ruff**

```bash
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 2.6: Commit**

```bash
git add src/door_sync/unifi/__init__.py src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add UniFi client package with Wiegand-26 nfc_id helpers"
```

---

## Task 3: Name-split and card-ID redaction helpers

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_split_name` and `_redact`)
- Modify: `tests/test_unifi_client.py` (add tests)

### Background

UniFi requires `first_name` AND `last_name` on user creation. CiviCRM gives us `display_name` as one string. `_split_name` splits on the last space; single-word names get `"—"` as the placeholder last_name (visibly distinct in the UniFi UI so an operator notices). `_redact` produces the last-4 form (`"****1234"`) used everywhere a card_id appears in a log line.

- [ ] **Step 3.1: Add `_split_name` and `_redact` to `src/door_sync/unifi/client.py`**

Append at the bottom of the file:

```python
def _split_name(display_name: str) -> tuple[str, str]:
    """Split a CiviCRM display_name into (first_name, last_name) for UniFi.

    Splits on the last space: 'Mary Anne Doe' -> ('Mary Anne', 'Doe').
    Single-word names get '—' as a placeholder last_name (UniFi requires
    both on create; the em-dash is visibly distinct so an operator
    notices and can edit in CiviCRM).
    """
    if not display_name:
        raise ValueError("display_name must be non-empty")
    if " " not in display_name:
        return (display_name, "—")
    first, _, last = display_name.rpartition(" ")
    return (first, last)


def _redact(card_id: int | None) -> str:
    """Return a last-4 redacted form of a card_id for log lines.

    None -> 'none'. Card_id -> '****NNNN' (zero-padded to 4 digits).
    Architecture §11: never log a full card_id at any level.
    """
    if card_id is None:
        return "none"
    return f"****{card_id % 10000:04d}"
```

- [ ] **Step 3.2: Update the imports at the top of `tests/test_unifi_client.py`**

Change the import line to also bring in the new helpers:

```python
from door_sync.unifi.client import (
    _compute_nfc_id,
    _parse_nfc_id,
    _redact,
    _split_name,
)
```

- [ ] **Step 3.3: Add the name-split tests**

Append to `tests/test_unifi_client.py`:

```python
# --- Name splitting ---


def test_split_name_two_words() -> None:
    assert _split_name("Jane Doe") == ("Jane", "Doe")


def test_split_name_three_words_splits_on_last_space() -> None:
    """A middle name or compound first name belongs with first_name."""
    assert _split_name("Mary Anne Doe") == ("Mary Anne", "Doe")


def test_split_name_single_word_pads_last_name() -> None:
    """UniFi requires both fields on create; em-dash flags it for review."""
    assert _split_name("Madonna") == ("Madonna", "—")


def test_split_name_empty_string_raises() -> None:
    """Empty display_name should never reach us; defensive."""
    import pytest
    with pytest.raises(ValueError):
        _split_name("")
```

- [ ] **Step 3.4: Add the redaction tests**

Append to `tests/test_unifi_client.py`:

```python
# --- Card-ID redaction ---


def test_redact_none() -> None:
    assert _redact(None) == "none"


def test_redact_short_card_id_zero_pads() -> None:
    """Card 7 redacts to ****0007, not ****7."""
    assert _redact(7) == "****0007"


def test_redact_full_width_card_id() -> None:
    assert _redact(1234) == "****1234"


def test_redact_strips_high_digits() -> None:
    """Only the last 4 digits ever appear in logs."""
    assert _redact(98765) == "****8765"
```

- [ ] **Step 3.5: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: ALL tests PASS (helpers + previous task's helpers).

- [ ] **Step 3.6: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add _split_name and _redact helpers to UniFi client"
```

---

## Task 4: Class skeleton, TLS fingerprint pinning, and context manager

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `UnifiClient`, `UnifiClientError`, TLS verification)
- Modify: `tests/test_unifi_client.py` (TLS + context-manager tests)

### Background

The constructor pins the TLS fingerprint before constructing the `httpx.Client`. Approach: open a raw socket, SSL-wrap with `CERT_NONE`, fetch the peer cert in binary form, SHA-256 it, compare to `config.tls_fingerprint`. Mismatch → raise; match → build the httpx client with `verify=False` for the cycle.

- [ ] **Step 4.1: Write the failing test for TLS-mismatch raising**

Add to `tests/test_unifi_client.py`:

```python
# --- Construction / TLS ---

import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from door_sync.config import UnifiConfig
from door_sync.unifi.client import UnifiClient, UnifiClientError


def _unifi_config(fingerprint: str = "AA" * 32) -> UnifiConfig:
    return UnifiConfig(
        host="192.0.2.1",
        api_key="testkey",
        tls_fingerprint=fingerprint,
        facility_code=42,
    )


def _patched_tls(cert_der: bytes) -> Any:
    """Context-manager that stubs socket+ssl to return cert_der as peer cert."""
    mock_ssock = MagicMock()
    mock_ssock.getpeercert.return_value = cert_der
    mock_ssock.__enter__.return_value = mock_ssock
    mock_ssock.__exit__.return_value = None

    mock_ctx = MagicMock()
    mock_ctx.wrap_socket.return_value = mock_ssock

    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    mock_sock.__exit__.return_value = None

    return patch.multiple(
        "door_sync.unifi.client",
        socket=MagicMock(create_connection=MagicMock(return_value=mock_sock)),
        ssl=MagicMock(SSLContext=MagicMock(return_value=mock_ctx), CERT_NONE=0, PROTOCOL_TLS_CLIENT=0),
    )


def test_init_raises_on_tls_fingerprint_mismatch() -> None:
    """Wrong fingerprint at init must raise before httpx.Client is built."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    wrong_fp = "BB" * 32
    assert real_fp != wrong_fp
    config = _unifi_config(fingerprint=wrong_fp)
    with _patched_tls(real_cert):
        with pytest.raises(UnifiClientError) as exc_info:
            UnifiClient(config)
        assert "TLS fingerprint mismatch" in str(exc_info.value)
```

- [ ] **Step 4.2: Run test, confirm it fails**

```bash
uv run pytest tests/test_unifi_client.py::test_init_raises_on_tls_fingerprint_mismatch -v
```

Expected: FAIL with `ImportError`/`AttributeError` because `UnifiClient` and `UnifiClientError` don't exist yet.

- [ ] **Step 4.3: Add `UnifiClient`, `UnifiClientError`, and TLS verification**

Add to `src/door_sync/unifi/client.py`, near the top after the module docstring:

```python
import hashlib
import socket
import ssl
from types import TracebackType

import httpx

from door_sync.config import UnifiConfig


_UNIFI_PORT = 12445


class UnifiClientError(Exception):
    """Raised on non-recoverable UniFi Access API failure."""


class UnifiClient:
    """Read+write UniFi Access local-API client.

    Construct one per reconcile cycle. Use as a context manager, or call
    close() explicitly. Honors a dry_run flag that turns writes into
    redacted log lines (architecture §5).
    """

    def __init__(self, config: UnifiConfig, *, dry_run: bool = False) -> None:
        self._config = config
        self._dry_run = dry_run
        self._verify_tls_fingerprint()
        self._http = httpx.Client(
            base_url=f"https://{config.host}:{_UNIFI_PORT}",
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=False,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    def _verify_tls_fingerprint(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection(
            (self._config.host, _UNIFI_PORT), timeout=10
        ) as raw:
            with ctx.wrap_socket(raw, server_hostname=self._config.host) as wrapped:
                cert_der = wrapped.getpeercert(binary_form=True)
        if cert_der is None:
            raise UnifiClientError("TLS handshake produced no peer certificate")
        actual_fp = hashlib.sha256(cert_der).hexdigest().lower()
        expected_fp = (
            self._config.tls_fingerprint.lower().replace(":", "")
        )
        if actual_fp != expected_fp:
            raise UnifiClientError(
                f"TLS fingerprint mismatch: expected {expected_fp[:16]}…, "
                f"got {actual_fp[:16]}…"
            )

    def close(self) -> None:
        # httpx.Client may not exist if __init__ failed before constructing it.
        http = getattr(self, "_http", None)
        if http is not None:
            http.close()

    def __enter__(self) -> "UnifiClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
```

- [ ] **Step 4.4: Run TLS-mismatch test, confirm it passes**

```bash
uv run pytest tests/test_unifi_client.py::test_init_raises_on_tls_fingerprint_mismatch -v
```

Expected: PASS.

- [ ] **Step 4.5: Add the TLS-match test**

Add to `tests/test_unifi_client.py`:

```python
def test_init_verifies_tls_fingerprint_match() -> None:
    """Matching fingerprint at init constructs the client successfully."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    config = _unifi_config(fingerprint=real_fp)
    with _patched_tls(real_cert):
        client = UnifiClient(config)
    assert client._http is not None
    client.close()


def test_init_accepts_colon_separated_fingerprint() -> None:
    """The fingerprint can be passed as AA:BB:CC:... (common format)."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    colon_form = ":".join(real_fp[i : i + 2] for i in range(0, len(real_fp), 2))
    config = _unifi_config(fingerprint=colon_form)
    with _patched_tls(real_cert):
        client = UnifiClient(config)
    client.close()
```

- [ ] **Step 4.6: Add the context-manager test**

Add to `tests/test_unifi_client.py`:

```python
def test_context_manager_closes_http_client() -> None:
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    config = _unifi_config(fingerprint=real_fp)
    with _patched_tls(real_cert):
        with UnifiClient(config) as client:
            assert client._http.is_closed is False
    assert client._http.is_closed is True
```

- [ ] **Step 4.7: Run all UniFi tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: ALL PASS.

- [ ] **Step 4.8: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add UnifiClient class with TLS fingerprint pinning"
```

---

## Task 5: Response-envelope unwrapping and retry helper

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_unwrap`, `_with_retries`, `_backoff_seconds`, `_parse_retry_after`)
- Modify: `tests/test_unifi_client.py` (envelope + retry tests via `pytest-httpx`)

### Background

Every UniFi API response wraps the payload in `{code, msg, data}`. `_unwrap` validates `code == "SUCCESS"` and returns `data`. `_with_retries` mirrors `civicrm/client.py`'s retry loop: 3 attempts, exponential backoff with ±20% jitter, honors `Retry-After` on 429, retries 5xx and network errors, no-retries 4xx (including 402). Re-use the same `_backoff_seconds` and `_parse_retry_after` helper shapes.

- [ ] **Step 5.1: Write failing test for non-SUCCESS envelope**

Add to `tests/test_unifi_client.py`:

```python
# --- Response envelope + retries ---

import json as _json
from pathlib import Path
from typing import Callable

from pytest_httpx import HTTPXMock


def _make_client(httpx_mock: HTTPXMock) -> UnifiClient:
    """Build a UnifiClient with TLS verification stubbed out."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        return UnifiClient(config)


def test_non_success_envelope_raises(httpx_mock: HTTPXMock) -> None:
    """code != SUCCESS raises UnifiClientError with the code + msg."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={"code": "CODE_AUTH_FAILED", "msg": "Authentication failed.", "data": None},
    )
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "CODE_AUTH_FAILED" in str(exc_info.value)
    assert "Authentication failed." in str(exc_info.value)
    client.close()
```

This test will fail because `fetch_users()` doesn't exist yet — that's intentional. We'll add `fetch_users` in Task 6 and revisit. For now, the test pins behavior we want; it will start passing in Task 6.

- [ ] **Step 5.2: Add `_unwrap`, retry helpers, and a generic `_request` method**

Add to the top imports of `src/door_sync/unifi/client.py`:

```python
import random
import time
from collections.abc import Callable
from typing import Any
```

Add constants near `_UNIFI_PORT`:

```python
_MAX_ATTEMPTS = 3
_MAX_PAGES = 1_000
_PAGE_SIZE = 100
```

Append these methods to the `UnifiClient` class (before `close`):

```python
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one API call with retries; unwrap the response envelope.

        Returns the `data` field on SUCCESS; raises UnifiClientError otherwise.
        """
        def _do() -> httpx.Response:
            return self._http.request(
                method, path, params=params, json=json, files=files
            )
        response = self._with_retries(_do)
        return self._unwrap(response)

    def _unwrap(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except (ValueError, _json.JSONDecodeError) as e:
            raise UnifiClientError(
                f"malformed JSON from {response.url}: {e}"
            ) from e
        if not isinstance(payload, dict):
            raise UnifiClientError(
                f"unexpected envelope shape from {response.url}: "
                f"expected object, got {type(payload).__name__}"
            )
        code = payload.get("code")
        if code != "SUCCESS":
            msg = payload.get("msg", "")
            raise UnifiClientError(f"{code}: {msg}")
        return payload.get("data")

    def _with_retries(
        self, action: Callable[[], httpx.Response]
    ) -> httpx.Response:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = action()
            except httpx.RequestError as e:
                if attempt == _MAX_ATTEMPTS:
                    raise UnifiClientError(
                        f"network failure after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code == 429:
                if attempt == _MAX_ATTEMPTS:
                    raise UnifiClientError(
                        f"HTTP 429 after {_MAX_ATTEMPTS} attempts: "
                        f"{response.text[:200]}"
                    )
                wait = _parse_retry_after(response) or _backoff_seconds(attempt)
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                if attempt == _MAX_ATTEMPTS:
                    raise UnifiClientError(
                        f"HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts: "
                        f"{response.text[:200]}"
                    )
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code >= 400:
                # 4xx other than 429 (including non-standard 402) → permanent.
                raise UnifiClientError(
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )

            return response

        raise UnifiClientError("retry loop exited unexpectedly")
```

Append the module-level helpers (after `_redact`):

```python
def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with ±20% jitter. attempt is 1-indexed."""
    base = float(2 ** (attempt - 1))
    jitter = random.uniform(-0.2, 0.2) * base
    return max(0.1, base + jitter)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header. Returns positive seconds if numeric.

    HTTP-date form is not supported (per spec) and returns None.
    Negative and zero values return None so the caller falls back to backoff.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if result > 0 else None
```

- [ ] **Step 5.3: Add tests for retry behavior**

Append to `tests/test_unifi_client.py`:

```python
def test_http_500_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three consecutive 500s exhaust retries and raise."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
            status_code=500,
            text="server error",
        )
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 500" in str(exc_info.value)
    client.close()


def test_http_402_raises_immediately_no_retry(httpx_mock: HTTPXMock) -> None:
    """402 'Request Failed' is non-standard 4xx; no retries."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        status_code=402,
        text="request failed",
    )
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 402" in str(exc_info.value)
    # Only one request should have been made.
    assert len(httpx_mock.get_requests()) == 1
    client.close()


def test_http_429_honors_retry_after_seconds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 with Retry-After: 5 waits >= 5 seconds, then 200 succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s)
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        status_code=429,
        headers={"Retry-After": "5"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={"code": "SUCCESS", "data": [], "msg": "success", "pagination": {"page_num": 1, "page_size": 100, "total": 0}},
    )
    client = _make_client(httpx_mock)
    client.fetch_users()
    assert any(s >= 5 for s in sleeps)
    client.close()


def test_malformed_json_raises(httpx_mock: HTTPXMock) -> None:
    """200 with non-JSON body raises UnifiClientError."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        text="<html>not json</html>",
    )
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "malformed JSON" in str(exc_info.value)
    client.close()
```

These tests reference `fetch_users()` which still doesn't exist — they'll fail. That's fine; Task 6 adds it.

- [ ] **Step 5.4: Run only the retry-helper module-level helper test (the only one not depending on fetch_users)**

There isn't a standalone one yet — write a quick one to confirm `_with_retries` plumbing at least compiles cleanly:

```bash
uv run mypy --strict src tests
uv run ruff check .
```

Expected: PASS. Tests that depend on `fetch_users` will fail at runtime but mypy/ruff should be clean.

- [ ] **Step 5.5: Commit (the partial state)**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add UniFi response envelope unwrapping and retry helper"
```

(Tests depending on `fetch_users` are expected red until Task 6 — that's the next step.)

---

## Task 6: `fetch_users()` happy path and pagination

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `fetch_users` + `UnifiUser` import)
- Modify: `tests/test_unifi_client.py` (fetch_users tests)

### Background

`fetch_users()` does `GET /api/v1/developer/users?page_num=N&page_size=100&expand[]=access_policy`, paginated until a page has fewer than `page_size` rows. For each row, parse `employee_number` as int (skip non-int), use `nfc_cards[0]` (with warning if multiple), parse `nfc_id` via `_parse_nfc_id`, take `access_policy_ids[0]` (with warning if multiple), build a `UnifiUser`. Cache `unifi_user_id` and `nfc_cards` per contact for use in `apply()` later.

- [ ] **Step 6.1: Add `UnifiUser` import and instance caches to `__init__`**

In `src/door_sync/unifi/client.py`, add to imports:

```python
import logging

from door_sync.models import UnifiUser
```

Add at module top:

```python
logger = logging.getLogger(__name__)
```

Inside `UnifiClient.__init__`, after constructing `self._http`, initialize the caches:

```python
        self._unifi_user_id_by_contact: dict[int, str] = {}
        self._nfc_cards_by_contact: dict[int, list[dict[str, Any]]] = {}
        self._nfc_token_map: dict[int, str] | None = None
        self._fetched_users_done = False
```

(Drop the `# noqa` if your linter complains — these are real state.)

- [ ] **Step 6.2: Write the failing happy-path test**

Append to `tests/test_unifi_client.py`:

```python
# --- fetch_users ---


def _user_row(
    contact_id: int = 42,
    user_id: str = "uuid-42",
    first_name: str = "Jane",
    last_name: str = "Doe",
    status: str = "ACTIVE",
    nfc_id: str = "2A04D2",
    policy_id: str = "pol-1",
    nfc_token: str = "tok-42",
) -> dict[str, Any]:
    return {
        "id": user_id,
        "first_name": first_name,
        "last_name": last_name,
        "employee_number": str(contact_id),
        "status": status,
        "nfc_cards": [{"id": "100001", "nfc_id": nfc_id, "token": nfc_token}],
        "access_policy_ids": [policy_id],
    }


def _users_page(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {
        "code": "SUCCESS",
        "msg": "success",
        "data": rows,
        "pagination": {
            "page_num": 1,
            "page_size": _PAGE_SIZE if total is None else min(100, total),
            "total": len(rows) if total is None else total,
        },
    }


_PAGE_SIZE = 100  # mirror the constant for the URL template


def test_fetch_users_happy_path(httpx_mock: HTTPXMock) -> None:
    """One page, returns list[UnifiUser] with parsed fields."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42)]),
    )
    client = _make_client(httpx_mock)
    users = client.fetch_users()
    assert len(users) == 1
    u = users[0]
    assert u.contact_id == 42
    assert u.display_name == "Jane Doe"
    assert u.card_id == 1234  # 2A04D2 decoded with FC=42 -> CN=1234
    assert u.active is True
    assert u.policy == "pol-1"
    client.close()
```

- [ ] **Step 6.3: Implement `fetch_users`**

Append to the `UnifiClient` class (before `close`):

```python
    def fetch_users(self) -> list[UnifiUser]:
        results: list[UnifiUser] = []
        for page_num in range(1, _MAX_PAGES + 1):
            data = self._request(
                "GET",
                "/api/v1/developer/users",
                params={
                    "page_num": page_num,
                    "page_size": _PAGE_SIZE,
                    "expand[]": "access_policy",
                },
            )
            if not isinstance(data, list):
                raise UnifiClientError(
                    f"expected list of users from /users, got {type(data).__name__}"
                )
            for row in data:
                user = self._row_to_unifi_user(row)
                if user is not None:
                    results.append(user)
            if len(data) < _PAGE_SIZE:
                self._fetched_users_done = True
                return results
        raise UnifiClientError(
            f"/users pagination exceeded {_MAX_PAGES} pages without terminating"
        )

    def _row_to_unifi_user(self, row: dict[str, Any]) -> UnifiUser | None:
        emp_raw = row.get("employee_number") or ""
        try:
            contact_id = int(emp_raw)
        except (ValueError, TypeError):
            return None
        user_id = str(row.get("id", ""))
        if not user_id:
            return None
        self._unifi_user_id_by_contact[contact_id] = user_id

        nfc_cards = row.get("nfc_cards") or []
        if not isinstance(nfc_cards, list):
            nfc_cards = []
        self._nfc_cards_by_contact[contact_id] = list(nfc_cards)

        card_id: int | None = None
        if len(nfc_cards) > 1:
            logger.warning(
                "contact %d has %d cards in UniFi; using the first",
                contact_id,
                len(nfc_cards),
            )
        if nfc_cards:
            nfc_id_raw = str(nfc_cards[0].get("nfc_id", ""))
            card_id = _parse_nfc_id(nfc_id_raw, self._config.facility_code)
            if card_id is None and nfc_id_raw:
                logger.warning(
                    "contact %d has foreign-FC card nfc_id=%s; treating as no card",
                    contact_id,
                    _redact(None),  # no card_id to redact — log presence only
                )

        policies = row.get("access_policy_ids") or []
        if not isinstance(policies, list):
            policies = []
        if len(policies) > 1:
            logger.warning(
                "contact %d has %d access policies; using the first",
                contact_id,
                len(policies),
            )
        policy = str(policies[0]) if policies else None

        first_name = str(row.get("first_name", ""))
        last_name = str(row.get("last_name", ""))
        display_name = " ".join(part for part in [first_name, last_name] if part).strip()
        active = str(row.get("status", "")) == "ACTIVE"

        return UnifiUser(
            contact_id=contact_id,
            display_name=display_name,
            card_id=card_id,
            active=active,
            policy=policy,
        )
```

- [ ] **Step 6.4: Run the happy-path test, confirm it passes**

```bash
uv run pytest tests/test_unifi_client.py::test_fetch_users_happy_path -v
```

Expected: PASS.

- [ ] **Step 6.5: Add the pagination test**

Append to `tests/test_unifi_client.py`:

```python
def test_fetch_users_paginates(httpx_mock: HTTPXMock) -> None:
    """101 users across 2 pages; follows until short page."""
    page1 = [_user_row(contact_id=i, user_id=f"uuid-{i}") for i in range(1, 101)]
    page2 = [_user_row(contact_id=101, user_id="uuid-101")]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(page1, total=101),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=2&page_size=100&expand[]=access_policy",
        json=_users_page(page2, total=101),
    )
    client = _make_client(httpx_mock)
    users = client.fetch_users()
    assert len(users) == 101
    assert {u.contact_id for u in users} == set(range(1, 102))
    client.close()
```

- [ ] **Step 6.6: Add the "skip admin / non-int employee_number" tests**

Append:

```python
def test_fetch_users_skips_admin_without_employee_number(
    httpx_mock: HTTPXMock,
) -> None:
    rows = [
        _user_row(contact_id=42),
        {**_user_row(contact_id=0), "employee_number": ""},  # admin
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(rows),
    )
    client = _make_client(httpx_mock)
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}
    client.close()


def test_fetch_users_skips_non_int_employee_number(
    httpx_mock: HTTPXMock,
) -> None:
    rows = [
        _user_row(contact_id=42),
        {**_user_row(contact_id=0), "employee_number": "bob"},
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(rows),
    )
    client = _make_client(httpx_mock)
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}
    client.close()
```

- [ ] **Step 6.7: Add the multi-card / multi-policy warning tests**

Append:

```python
def test_fetch_users_logs_warning_on_multiple_cards(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    row = _user_row(contact_id=42)
    row["nfc_cards"] = [
        {"id": "100001", "nfc_id": "2A04D2", "token": "tok-1"},
        {"id": "100002", "nfc_id": "2A04D3", "token": "tok-2"},
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    with caplog.at_level(logging.WARNING, logger="door_sync.unifi.client"):
        client = _make_client(httpx_mock)
        users = client.fetch_users()
    assert users[0].card_id == 1234  # uses the first card
    assert any("2 cards" in rec.message for rec in caplog.records)
    client.close()


def test_fetch_users_foreign_fc_card_yields_card_id_none(
    httpx_mock: HTTPXMock,
) -> None:
    """A card with a non-configured facility code → card_id=None on the user."""
    row = _user_row(contact_id=42, nfc_id="990000")  # FC=99, not 42
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    client = _make_client(httpx_mock)
    users = client.fetch_users()
    assert users[0].card_id is None
    client.close()
```

- [ ] **Step 6.8: Re-run the envelope/retry tests from Task 5 — they should now pass too**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: ALL PASS, including the previously-red tests for `test_non_success_envelope_raises`, `test_http_500_retries_then_raises`, etc.

- [ ] **Step 6.9: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Implement UnifiClient.fetch_users with pagination"
```

---

## Task 7: `apply()` precondition + dry-run skeleton

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `apply` shell)
- Modify: `tests/test_unifi_client.py` (precondition + dry-run tests)

### Background

Implement `apply()` as a shell that (a) enforces the "must call fetch_users first" precondition and (b) handles dry-run by emitting redacted log lines for every intended action and making zero HTTP writes. Subsequent tasks fill in each diff bucket's actual write logic.

- [ ] **Step 7.1: Add `Diff` import**

In `src/door_sync/unifi/client.py`, extend the models import:

```python
from door_sync.models import Diff, ResolvedMember, UnifiUser
```

- [ ] **Step 7.2: Write failing precondition test**

Append to `tests/test_unifi_client.py`:

```python
# --- apply preconditions & dry-run ---

from door_sync.models import Diff, ResolvedMember


def _diff(
    to_add: list[ResolvedMember] | None = None,
    to_update_credential: list[tuple[ResolvedMember, UnifiUser]] | None = None,
    to_update_policy: list[tuple[ResolvedMember, UnifiUser]] | None = None,
    to_deactivate: list[UnifiUser] | None = None,
    unmapped: list[ResolvedMember] | None = None,
) -> Diff:
    return Diff(
        to_add=to_add or [],
        to_update_credential=to_update_credential or [],
        to_update_policy=to_update_policy or [],
        to_deactivate=to_deactivate or [],
        unmapped=unmapped or [],
    )


def _resolved(contact_id: int, card_id: int | None = 1234, target_policy: str = "pol-1") -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=f"Member {contact_id}",
        card_id=card_id,
        target_policy=target_policy,
        resolution="tier",
    )


def _unifi_user(contact_id: int, card_id: int | None = 1234, active: bool = True, policy: str | None = "pol-1") -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=f"Member {contact_id}",
        card_id=card_id,
        active=active,
        policy=policy,
    )


def test_apply_requires_prior_fetch_users(httpx_mock: HTTPXMock) -> None:
    """Calling apply() before fetch_users() must raise."""
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.apply(_diff(to_deactivate=[_unifi_user(99)]))
    assert "fetch_users" in str(exc_info.value)
    client.close()
```

- [ ] **Step 7.3: Add `apply()` shell**

Append to the `UnifiClient` class (before `close`):

```python
    def apply(self, diff: Diff) -> None:
        """Apply a diff to UniFi Access.

        Precondition: fetch_users() must have been called on this instance
        first (the orchestrator's flow enforces this). The cached
        unifi_user_id and nfc_cards maps it populates are required.
        """
        if not self._fetched_users_done:
            raise UnifiClientError(
                "apply() requires a prior fetch_users() call on the same instance"
            )
        if self._dry_run:
            self._log_dry_run_actions(diff)
            return
        # Live writes — implemented in Tasks 8-12.
        raise UnifiClientError(
            "live apply() not yet implemented (this branch should be unreachable)"
        )

    def _log_dry_run_actions(self, diff: Diff) -> None:
        for member in diff.to_add:
            logger.info(
                "would-add contact=%d card=%s policy=%s",
                member.contact_id,
                _redact(member.card_id),
                member.target_policy,
            )
        for resolved, unifi_user in diff.to_update_credential:
            logger.info(
                "would-update-credential contact=%d old_card=%s new_card=%s",
                resolved.contact_id,
                _redact(unifi_user.card_id),
                _redact(resolved.card_id),
            )
        for resolved, unifi_user in diff.to_update_policy:
            logger.info(
                "would-update-policy contact=%d old=%s new=%s",
                resolved.contact_id,
                unifi_user.policy,
                resolved.target_policy,
            )
        for unifi_user in diff.to_deactivate:
            logger.info(
                "would-deactivate contact=%d card=%s",
                unifi_user.contact_id,
                _redact(unifi_user.card_id),
            )
```

- [ ] **Step 7.4: Confirm precondition test passes**

```bash
uv run pytest tests/test_unifi_client.py::test_apply_requires_prior_fetch_users -v
```

Expected: PASS.

- [ ] **Step 7.5: Add dry-run test**

Append:

```python
def test_apply_dry_run_makes_no_writes(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-empty diff in dry-run logs intentions but issues zero httpx writes."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config, dry_run=True)

    # Seed the precondition: a fetch_users that returns empty.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    diff = _diff(
        to_add=[_resolved(1)],
        to_deactivate=[_unifi_user(2)],
    )
    with caplog.at_level(logging.INFO, logger="door_sync.unifi.client"):
        client.apply(diff)

    # Only the one fetch_users GET should have been issued — no writes.
    assert len(httpx_mock.get_requests()) == 1
    # Two log lines: would-add and would-deactivate.
    messages = [r.message for r in caplog.records]
    assert any("would-add" in m for m in messages)
    assert any("would-deactivate" in m for m in messages)
    # Card IDs are redacted.
    assert any("****1234" in m for m in messages)
    assert not any("1234 " in m and "****" not in m for m in messages)
    client.close()
```

- [ ] **Step 7.6: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: ALL PASS.

- [ ] **Step 7.7: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add apply() precondition check and dry-run logging"
```

---

## Task 8: NFC token-map fetch

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_ensure_nfc_token_map`)
- Modify: `tests/test_unifi_client.py` (token-map tests)

### Background

The NFC token map is `dict[card_id_int → token_str]`, populated lazily on the first time `apply()` needs to bind a card. It comes from `GET /api/v1/developer/credentials/nfc_cards/tokens?page_num=N&page_size=100`. Rows whose nfc_id doesn't decode under the configured facility code are skipped (admin-managed / foreign-FC cards).

- [ ] **Step 8.1: Add `_ensure_nfc_token_map`**

Append to the `UnifiClient` class:

```python
    def _ensure_nfc_token_map(self) -> dict[int, str]:
        if self._nfc_token_map is not None:
            return self._nfc_token_map
        token_map: dict[int, str] = {}
        for page_num in range(1, _MAX_PAGES + 1):
            data = self._request(
                "GET",
                "/api/v1/developer/credentials/nfc_cards/tokens",
                params={"page_num": page_num, "page_size": _PAGE_SIZE},
            )
            if not isinstance(data, list):
                raise UnifiClientError(
                    f"expected list of cards from /nfc_cards/tokens, "
                    f"got {type(data).__name__}"
                )
            for row in data:
                nfc_id = str(row.get("nfc_id", ""))
                token = str(row.get("token", ""))
                if not nfc_id or not token:
                    continue
                card_id = _parse_nfc_id(nfc_id, self._config.facility_code)
                if card_id is None:
                    logger.debug(
                        "skipping foreign-FC or unparseable card nfc_id=%s",
                        nfc_id,
                    )
                    continue
                token_map[card_id] = token
            if len(data) < _PAGE_SIZE:
                break
        else:
            raise UnifiClientError(
                f"/nfc_cards/tokens pagination exceeded {_MAX_PAGES} pages"
            )
        self._nfc_token_map = token_map
        return token_map
```

- [ ] **Step 8.2: Write the token-map test**

Append to `tests/test_unifi_client.py`:

```python
# --- NFC token map ---


def _cards_page(
    rows: list[dict[str, Any]], total: int | None = None
) -> dict[str, Any]:
    return {
        "code": "SUCCESS",
        "msg": "success",
        "data": rows,
        "pagination": {
            "page_num": 1,
            "page_size": 100,
            "total": len(rows) if total is None else total,
        },
    }


def test_token_map_keys_by_parsed_card_id(httpx_mock: HTTPXMock) -> None:
    """Build dict[card_id → token]; foreign-FC and unparseable rows are skipped."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([
            {"nfc_id": "2A04D2", "token": "tok-1234", "display_id": "100001"},
            {"nfc_id": "2A04D3", "token": "tok-1235", "display_id": "100002"},
            {"nfc_id": "990000", "token": "tok-foreign", "display_id": "100003"},
            {"nfc_id": "not-hex", "token": "tok-bad", "display_id": "100004"},
        ]),
    )
    token_map = client._ensure_nfc_token_map()
    assert token_map == {1234: "tok-1234", 1235: "tok-1235"}
    client.close()


def test_token_map_cached_across_calls(httpx_mock: HTTPXMock) -> None:
    """Second call doesn't re-fetch."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    first = client._ensure_nfc_token_map()
    second = client._ensure_nfc_token_map()
    assert first is second
    assert len(httpx_mock.get_requests()) == 1
    client.close()
```

- [ ] **Step 8.3: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 8.4: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add lazy NFC token-map fetch keyed by parsed card_id"
```

---

## Task 9: Card import (2-column CSV upload)

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_import_cards`)
- Modify: `tests/test_unifi_client.py` (import tests)

### Background

When a card_id appears in the diff but not in the token map, we register it via the documented 2-column API CSV. Body shape: one line per card, `<nfc_id>,<alias>`, no header. Multipart field name `file`, content-type `text/csv`. The response data is `[{nfc_id, alias, token}, ...]`; empty token signals per-row failure.

- [ ] **Step 9.1: Add `_import_cards` method**

Append to the `UnifiClient` class:

```python
    def _import_cards(self, card_ids: list[int]) -> None:
        """Register a batch of cards via CSV upload; update the token map.

        Per spec §9: 2-column CSV (`<nfc_id>,<alias>`), no header; multipart
        upload via field name `file`. On per-row failure (empty token in
        response), raise immediately.
        """
        if not card_ids:
            return
        token_map = self._ensure_nfc_token_map()
        lines: list[str] = []
        for card_id in card_ids:
            nfc_id = _compute_nfc_id(self._config.facility_code, card_id)
            alias = f"sync-{card_id:05d}"
            lines.append(f"{nfc_id},{alias}")
        csv_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        data = self._request(
            "POST",
            "/api/v1/developer/credentials/nfc_cards/import",
            files={"file": ("cards.csv", csv_bytes, "text/csv")},
        )
        if not isinstance(data, list):
            raise UnifiClientError(
                f"expected list from /nfc_cards/import, got {type(data).__name__}"
            )
        for row in data:
            nfc_id = str(row.get("nfc_id", ""))
            token = str(row.get("token", ""))
            card_id = _parse_nfc_id(nfc_id, self._config.facility_code)
            if card_id is None:
                raise UnifiClientError(
                    f"import returned card with wrong FC or unparseable nfc_id: {nfc_id!r}"
                )
            if not token:
                raise UnifiClientError(
                    f"card import failed for card_id={_redact(card_id)} (empty token in response)"
                )
            token_map[card_id] = token
```

- [ ] **Step 9.2: Add the import-CSV-format test**

Append to `tests/test_unifi_client.py`:

```python
# --- Card import ---


def test_import_cards_uses_2col_csv_format(httpx_mock: HTTPXMock) -> None:
    """Multipart body contains <nfc_id>,sync-<padded> lines, no header."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # First, an empty token-map fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    # Then the import.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [
                {"alias": "sync-01234", "nfc_id": "2A04D2", "token": "tok-1234"},
                {"alias": "sync-01235", "nfc_id": "2A04D3", "token": "tok-1235"},
            ],
        },
    )
    client._import_cards([1234, 1235])

    # Inspect the second request — the multipart body must contain our CSV.
    import_req = httpx_mock.get_requests()[1]
    body = import_req.content.decode("utf-8", errors="replace")
    assert "2A04D2,sync-01234" in body
    assert "2A04D3,sync-01235" in body
    # No header row.
    assert "nfc_id,alias" not in body
    # Token map updated.
    assert client._nfc_token_map == {1234: "tok-1234", 1235: "tok-1235"}
    client.close()


def test_import_cards_empty_token_raises(httpx_mock: HTTPXMock) -> None:
    """A row with empty token in the response signals a failed import."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01234", "nfc_id": "2A04D2", "token": ""}],
        },
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client._import_cards([1234])
    assert "card_id=****1234" in str(exc_info.value)
    client.close()


def test_import_cards_empty_list_is_noop(httpx_mock: HTTPXMock) -> None:
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)
    client._import_cards([])
    assert len(httpx_mock.get_requests()) == 0
    client.close()
```

- [ ] **Step 9.3: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 9.4: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Add NFC card batch import via 2-column CSV upload"
```

---

## Task 10: Live `apply()` — `to_deactivate` bucket

**Files:**
- Modify: `src/door_sync/unifi/client.py` (extend `apply` to handle to_deactivate)
- Modify: `tests/test_unifi_client.py` (deactivate test)

### Background

Replace the "live apply() not yet implemented" placeholder with actual logic. We'll add one bucket at a time; this task does `to_deactivate`. Per spec §8 step 3: PUT `/users/:id` with `{"status": "DEACTIVATED"}`.

- [ ] **Step 10.1: Replace `apply()`'s placeholder with the deactivate path**

In `src/door_sync/unifi/client.py`, find:

```python
        if self._dry_run:
            self._log_dry_run_actions(diff)
            return
        # Live writes — implemented in Tasks 8-12.
        raise UnifiClientError(
            "live apply() not yet implemented (this branch should be unreachable)"
        )
```

Replace with:

```python
        if self._dry_run:
            self._log_dry_run_actions(diff)
            return
        self._apply_deactivate(diff)
        # Other buckets implemented in subsequent tasks.

    _INTER_CALL_DELAY_SECONDS = 0.075

    def _apply_deactivate(self, diff: Diff) -> None:
        for unifi_user in diff.to_deactivate:
            user_id = self._unifi_user_id_by_contact.get(unifi_user.contact_id)
            if user_id is None:
                logger.warning(
                    "skipping deactivate for contact=%d: no cached user_id",
                    unifi_user.contact_id,
                )
                continue
            self._request(
                "PUT",
                f"/api/v1/developer/users/{user_id}",
                json={"status": "DEACTIVATED"},
            )
            time.sleep(self._INTER_CALL_DELAY_SECONDS)
```

(Inter-call delay applied per write call. The trailing sleep after each call means consecutive writes are spaced; we accept one trailing sleep at the very end of the cycle for simplicity.)

- [ ] **Step 10.2: Write the deactivate test**

Append to `tests/test_unifi_client.py`:

```python
# --- apply: live writes ---


def test_apply_deactivate_sets_status(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_deactivate → PUT /users/:id with status=DEACTIVATED."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)

    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Prime the cache via fetch_users.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42")]),
    )
    fetched = client.fetch_users()

    # The deactivate write.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    client.apply(_diff(to_deactivate=[fetched[0]]))

    write_req = httpx_mock.get_requests()[-1]
    body = _json.loads(write_req.content)
    assert body == {"status": "DEACTIVATED"}
    client.close()
```

- [ ] **Step 10.3: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 10.4: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Implement apply() to_deactivate bucket"
```

---

## Task 11: Live `apply()` — `to_update_credential` bucket

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_apply_update_credential`)
- Modify: `tests/test_unifi_client.py` (credential-update tests)

### Background

`to_update_credential` carries `(ResolvedMember, UnifiUser)` pairs. We update name (if changed) via PUT `/users/:id`, then swap cards: DELETE the old card via `/users/:id/nfc_cards/delete` (with the cached token), import the new card if it's not in the token map, then PUT bind it via `/users/:id/nfc_cards`.

- [ ] **Step 11.1: Add `_apply_update_credential`**

Append to the `UnifiClient` class:

```python
    def _apply_update_credential(self, diff: Diff) -> None:
        if not diff.to_update_credential:
            return
        # Pre-import any new cards in one batch.
        token_map = self._ensure_nfc_token_map()
        new_cards = [
            resolved.card_id
            for resolved, _ in diff.to_update_credential
            if resolved.card_id is not None and resolved.card_id not in token_map
        ]
        if new_cards:
            self._import_cards(new_cards)

        for resolved, unifi_user in diff.to_update_credential:
            user_id = self._unifi_user_id_by_contact.get(resolved.contact_id)
            if user_id is None:
                logger.warning(
                    "skipping update_credential for contact=%d: no cached user_id",
                    resolved.contact_id,
                )
                continue

            if resolved.display_name != unifi_user.display_name:
                first, last = _split_name(resolved.display_name)
                self._request(
                    "PUT",
                    f"/api/v1/developer/users/{user_id}",
                    json={"first_name": first, "last_name": last},
                )
                time.sleep(self._INTER_CALL_DELAY_SECONDS)

            if resolved.card_id != unifi_user.card_id:
                # Delete old card(s) on the user.
                for old_card in self._nfc_cards_by_contact.get(
                    resolved.contact_id, []
                ):
                    old_token = str(old_card.get("token", ""))
                    if not old_token:
                        continue
                    self._request(
                        "DELETE",
                        f"/api/v1/developer/users/{user_id}/nfc_cards/delete",
                        json={"token": old_token},
                    )
                    time.sleep(self._INTER_CALL_DELAY_SECONDS)
                # Bind new card if specified.
                if resolved.card_id is not None:
                    new_token = self._ensure_nfc_token_map().get(resolved.card_id)
                    if new_token is None:
                        raise UnifiClientError(
                            f"no token for card_id={_redact(resolved.card_id)} "
                            f"after import (contact={resolved.contact_id})"
                        )
                    self._request(
                        "PUT",
                        f"/api/v1/developer/users/{user_id}/nfc_cards",
                        json={"token": new_token, "force_add": False},
                    )
                    time.sleep(self._INTER_CALL_DELAY_SECONDS)
```

- [ ] **Step 11.2: Wire `_apply_update_credential` into `apply()`**

Find in `apply()`:

```python
        self._apply_deactivate(diff)
        # Other buckets implemented in subsequent tasks.
```

Replace with:

```python
        self._apply_deactivate(diff)
        self._apply_update_credential(diff)
        # Other buckets implemented in subsequent tasks.
```

- [ ] **Step 11.3: Write the credential-swap test**

Append to `tests/test_unifi_client.py`:

```python
def test_apply_update_credential_swaps_card(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_update_credential with changed card_id: DELETE old, PUT new."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)

    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Fetch returns user 42 with old card_id=1234 (nfc_id=2A04D2, token=tok-1234).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(
            contact_id=42, user_id="uuid-42", nfc_id="2A04D2", nfc_token="tok-1234",
        )]),
    )
    fetched = client.fetch_users()

    # Token map fetch (the new card is not yet in the map).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([
            {"nfc_id": "2A04D2", "token": "tok-1234"},
        ]),
    )
    # Import for the new card 1235.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01235", "nfc_id": "2A04D3", "token": "tok-1235"}],
        },
    )
    # DELETE old card.
    httpx_mock.add_response(
        method="DELETE",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards/delete",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT new card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1235)
    diff = _diff(to_update_credential=[(resolved, fetched[0])])
    client.apply(diff)

    # Verify the DELETE body referenced the OLD token.
    delete_req = next(
        r for r in httpx_mock.get_requests()
        if r.method == "DELETE" and r.url.path.endswith("/nfc_cards/delete")
    )
    assert _json.loads(delete_req.content) == {"token": "tok-1234"}
    # And the PUT body referenced the NEW token.
    bind_req = next(
        r for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path.endswith("/nfc_cards")
    )
    assert _json.loads(bind_req.content) == {"token": "tok-1235", "force_add": False}
    client.close()
```

- [ ] **Step 11.4: Add a name-only update test**

Append:

```python
def test_apply_update_credential_name_only(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """display_name changes but card_id doesn't: only PUT name, no card calls."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(
            contact_id=42, user_id="uuid-42",
            first_name="Old", last_name="Name", nfc_id="2A04D2",
        )]),
    )
    fetched = client.fetch_users()

    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="New Name",
        card_id=1234,  # same as fetched
        target_policy="pol-1",
        resolution="tier",
    )
    diff = _diff(to_update_credential=[(resolved, fetched[0])])
    client.apply(diff)

    put_req = httpx_mock.get_requests()[-1]
    body = _json.loads(put_req.content)
    assert body == {"first_name": "New", "last_name": "Name"}
    # No nfc_cards calls.
    nfc_calls = [
        r for r in httpx_mock.get_requests() if "nfc_cards" in str(r.url)
    ]
    assert nfc_calls == []
    client.close()
```

- [ ] **Step 11.5: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 11.6: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Implement apply() to_update_credential bucket"
```

---

## Task 12: Live `apply()` — `to_update_policy` bucket

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_apply_update_policy`)
- Modify: `tests/test_unifi_client.py` (policy-update test)

### Background

`PUT /users/:id/access_policies` with body `{"access_policy_ids": [<target_policy>]}` — single-element list, replaces all existing policies on the user.

- [ ] **Step 12.1: Add `_apply_update_policy`**

Append to the `UnifiClient` class:

```python
    def _apply_update_policy(self, diff: Diff) -> None:
        for resolved, _unifi_user in diff.to_update_policy:
            user_id = self._unifi_user_id_by_contact.get(resolved.contact_id)
            if user_id is None:
                logger.warning(
                    "skipping update_policy for contact=%d: no cached user_id",
                    resolved.contact_id,
                )
                continue
            if resolved.target_policy is None:
                logger.warning(
                    "skipping update_policy for contact=%d: target_policy is None",
                    resolved.contact_id,
                )
                continue
            self._request(
                "PUT",
                f"/api/v1/developer/users/{user_id}/access_policies",
                json={"access_policy_ids": [resolved.target_policy]},
            )
            time.sleep(self._INTER_CALL_DELAY_SECONDS)
```

- [ ] **Step 12.2: Wire into `apply()`**

Update the call sequence in `apply()`:

```python
        self._apply_deactivate(diff)
        self._apply_update_credential(diff)
        self._apply_update_policy(diff)
        # to_add implemented in next task.
```

- [ ] **Step 12.3: Write the policy-update test**

Append to `tests/test_unifi_client.py`:

```python
def test_apply_update_policy_replaces(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42", policy_id="pol-old")]),
    )
    fetched = client.fetch_users()

    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, target_policy="pol-new")
    diff = _diff(to_update_policy=[(resolved, fetched[0])])
    client.apply(diff)

    put_req = httpx_mock.get_requests()[-1]
    assert _json.loads(put_req.content) == {"access_policy_ids": ["pol-new"]}
    client.close()
```

- [ ] **Step 12.4: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 12.5: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Implement apply() to_update_policy bucket"
```

---

## Task 13: Live `apply()` — `to_add` bucket (create + reactivate paths)

**Files:**
- Modify: `src/door_sync/unifi/client.py` (add `_apply_add`)
- Modify: `tests/test_unifi_client.py` (add-bucket tests)

### Background

`to_add` covers both "user doesn't exist" and "user exists but inactive" cases (per architecture §8 diff table). The client distinguishes via the cached `_unifi_user_id_by_contact` map. For reactivation, if the cached user's previous card differs from `resolved.card_id`, the old card is deleted before the new one is bound (spec §8 step 6).

- [ ] **Step 13.1: Pre-batch card imports for the entire diff**

We want to import all unknown cards in ONE CSV upload across both `to_update_credential` and `to_add`. Refactor `_apply_update_credential` to skip pre-importing (we'll move pre-import to a new `_apply_add` helper that runs ahead of all bucket-specific code? No — simpler: do all the imports in `apply()` at the start, before any bucket).

Edit `apply()` to call a new helper first:

Find:
```python
        self._apply_deactivate(diff)
        self._apply_update_credential(diff)
        self._apply_update_policy(diff)
        # to_add implemented in next task.
```

Replace with:
```python
        self._preimport_unknown_cards(diff)
        self._apply_deactivate(diff)
        self._apply_update_credential(diff)
        self._apply_update_policy(diff)
        self._apply_add(diff)
```

Add `_preimport_unknown_cards`:

```python
    def _preimport_unknown_cards(self, diff: Diff) -> None:
        """Batch-import any card_ids needed by to_add or to_update_credential
        that aren't already in the token map.
        """
        token_map = self._ensure_nfc_token_map()
        needed: set[int] = set()
        for resolved in diff.to_add:
            if resolved.card_id is not None and resolved.card_id not in token_map:
                needed.add(resolved.card_id)
        for resolved, _unifi_user in diff.to_update_credential:
            if resolved.card_id is not None and resolved.card_id not in token_map:
                needed.add(resolved.card_id)
        if needed:
            self._import_cards(sorted(needed))
```

Now remove the redundant in-bucket pre-import from `_apply_update_credential`. Find this in `_apply_update_credential`:

```python
        # Pre-import any new cards in one batch.
        token_map = self._ensure_nfc_token_map()
        new_cards = [
            resolved.card_id
            for resolved, _ in diff.to_update_credential
            if resolved.card_id is not None and resolved.card_id not in token_map
        ]
        if new_cards:
            self._import_cards(new_cards)

```

Delete it. The pre-import in `apply()` covers this bucket too.

- [ ] **Step 13.2: Add `_apply_add`**

Append to the `UnifiClient` class:

```python
    def _apply_add(self, diff: Diff) -> None:
        for resolved in diff.to_add:
            existing_user_id = self._unifi_user_id_by_contact.get(resolved.contact_id)
            first, last = _split_name(resolved.display_name)
            if existing_user_id is not None:
                # Reactivate path
                self._reactivate_existing(resolved, existing_user_id, first, last)
            else:
                # True create
                user_id = self._create_user(resolved, first, last)
                self._unifi_user_id_by_contact[resolved.contact_id] = user_id
            # Common tail: bind card + assign policy.
            current_user_id = self._unifi_user_id_by_contact[resolved.contact_id]
            self._bind_card_if_set(current_user_id, resolved)
            self._assign_policy_if_set(current_user_id, resolved)

    def _reactivate_existing(
        self,
        resolved: ResolvedMember,
        user_id: str,
        first: str,
        last: str,
    ) -> None:
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}",
            json={
                "first_name": first,
                "last_name": last,
                "employee_number": str(resolved.contact_id),
                "status": "ACTIVE",
            },
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
        # Delete any old cards that differ from the new card_id.
        cached_cards = self._nfc_cards_by_contact.get(resolved.contact_id, [])
        new_token = (
            self._ensure_nfc_token_map().get(resolved.card_id)
            if resolved.card_id is not None
            else None
        )
        for old_card in cached_cards:
            old_token = str(old_card.get("token", ""))
            if not old_token or old_token == new_token:
                continue
            self._request(
                "DELETE",
                f"/api/v1/developer/users/{user_id}/nfc_cards/delete",
                json={"token": old_token},
            )
            time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _create_user(
        self, resolved: ResolvedMember, first: str, last: str
    ) -> str:
        data = self._request(
            "POST",
            "/api/v1/developer/users",
            json={
                "first_name": first,
                "last_name": last,
                "employee_number": str(resolved.contact_id),
            },
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
        if not isinstance(data, dict) or "id" not in data:
            raise UnifiClientError(
                f"POST /users returned no id for contact={resolved.contact_id}"
            )
        return str(data["id"])

    def _bind_card_if_set(self, user_id: str, resolved: ResolvedMember) -> None:
        if resolved.card_id is None:
            return
        token = self._ensure_nfc_token_map().get(resolved.card_id)
        if token is None:
            raise UnifiClientError(
                f"no token for card_id={_redact(resolved.card_id)} "
                f"after import (contact={resolved.contact_id})"
            )
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}/nfc_cards",
            json={"token": token, "force_add": False},
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)

    def _assign_policy_if_set(
        self, user_id: str, resolved: ResolvedMember
    ) -> None:
        if resolved.target_policy is None:
            return
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}/access_policies",
            json={"access_policy_ids": [resolved.target_policy]},
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
```

- [ ] **Step 13.3: Write the create-new-user test**

Append:

```python
def test_apply_create_new_user_path(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_add for unknown contact_id: POST /users, then bind card + assign policy."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Empty initial fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    # Token-map fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    # Import new card.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01234", "nfc_id": "2A04D2", "token": "tok-1234"}],
        },
    )
    # POST /users → returns the new user_id.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={
            "code": "SUCCESS", "msg": "success",
            "data": {"id": "uuid-new", "first_name": "Jane", "last_name": "Doe"},
        },
    )
    # PUT bind card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42, display_name="Jane Doe", card_id=1234,
        target_policy="pol-1", resolution="tier",
    )
    client.apply(_diff(to_add=[resolved]))

    post_user = next(
        r for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/developer/users"
    )
    body = _json.loads(post_user.content)
    assert body == {"first_name": "Jane", "last_name": "Doe", "employee_number": "42"}
    client.close()
```

- [ ] **Step 13.4: Write the reactivate-same-card test**

Append:

```python
def test_apply_reactivate_inactive_user_path(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_add for cached-inactive contact, same card: PUT ACTIVE, bind, assign — no DELETE."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Fetch returns user 42 inactive with the same card.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(
            contact_id=42, user_id="uuid-42",
            status="DEACTIVATED", nfc_id="2A04D2", nfc_token="tok-1234",
        )]),
    )
    client.fetch_users()

    # Token-map fetch (card already known).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    # PUT reactivate.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT bind card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1234)
    client.apply(_diff(to_add=[resolved]))

    # No DELETE calls.
    delete_calls = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
    assert delete_calls == []
    client.close()
```

- [ ] **Step 13.5: Write the reactivate-with-card-swap test**

Append:

```python
def test_apply_reactivate_swaps_card_when_changed(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_add for cached-inactive with different card_id: activate, DELETE old, bind new."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Fetch: inactive user with OLD card_id=1234.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(
            contact_id=42, user_id="uuid-42",
            status="DEACTIVATED", nfc_id="2A04D2", nfc_token="tok-1234",
        )]),
    )
    client.fetch_users()

    # Token-map: old card known, new one not.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    # Import for new card 1235.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS", "msg": "success",
            "data": [{"alias": "sync-01235", "nfc_id": "2A04D3", "token": "tok-1235"}],
        },
    )
    # PUT reactivate.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # DELETE old card.
    httpx_mock.add_response(
        method="DELETE",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards/delete",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT bind new.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1235)
    client.apply(_diff(to_add=[resolved]))

    # Confirm sequence: PUT user (reactivate) → DELETE old → PUT new card → PUT policy.
    methods_paths = [
        (r.method, r.url.path) for r in httpx_mock.get_requests()
        if r.url.path.startswith("/api/v1/developer/users/uuid-42")
    ]
    assert methods_paths == [
        ("PUT", "/api/v1/developer/users/uuid-42"),
        ("DELETE", "/api/v1/developer/users/uuid-42/nfc_cards/delete"),
        ("PUT", "/api/v1/developer/users/uuid-42/nfc_cards"),
        ("PUT", "/api/v1/developer/users/uuid-42/access_policies"),
    ]
    client.close()
```

- [ ] **Step 13.6: Run tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 13.7: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "Implement apply() to_add bucket with reactivate path"
```

---

## Task 14: Apply-order integration test + inter-call delay assertion

**Files:**
- Modify: `tests/test_unifi_client.py` (integration + delay test)

### Background

Now that every bucket is implemented, pin the order (deactivate → update_credential → update_policy → add) with one integration test, and assert `time.sleep(0.075)` is called between writes.

- [ ] **Step 14.1: Write the order-of-operations test**

Append:

```python
def test_apply_executes_deactivate_update_credential_update_policy_add_order(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single diff with one entry in each bucket; assert HTTPX call sequence."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # 4 users in fetch: 100 (deactivate), 101 (update_credential), 102 (update_policy), 103 (add reactivate).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([
            _user_row(contact_id=100, user_id="u100", nfc_id="2A04D2", nfc_token="t100"),
            _user_row(contact_id=101, user_id="u101", nfc_id="2A04D3", nfc_token="t101"),
            _user_row(contact_id=102, user_id="u102", nfc_id="2A04D4", nfc_token="t102", policy_id="old"),
        ]),
    )
    fetched = client.fetch_users()
    by_id = {u.contact_id: u for u in fetched}

    # Token-map fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([
            {"nfc_id": "2A04D2", "token": "t100"},
            {"nfc_id": "2A04D3", "token": "t101"},
            {"nfc_id": "2A04D4", "token": "t102"},
            {"nfc_id": "2A04D6", "token": "t1238"},  # for the new card on 101
        ]),
    )

    # Pre-set a bunch of generic SUCCESS responses for the writes.
    for url, method in [
        ("https://192.0.2.1:12445/api/v1/developer/users/u100", "PUT"),
        ("https://192.0.2.1:12445/api/v1/developer/users/u101/nfc_cards/delete", "DELETE"),
        ("https://192.0.2.1:12445/api/v1/developer/users/u101/nfc_cards", "PUT"),
        ("https://192.0.2.1:12445/api/v1/developer/users/u102/access_policies", "PUT"),
    ]:
        httpx_mock.add_response(
            method=method, url=url,
            json={"code": "SUCCESS", "msg": "success", "data": None},
        )

    diff = _diff(
        to_deactivate=[by_id[100]],
        to_update_credential=[(_resolved(101, card_id=1238), by_id[101])],
        to_update_policy=[(_resolved(102, target_policy="new"), by_id[102])],
        # no to_add (covered separately)
    )
    client.apply(diff)

    write_path_methods = [
        (r.method, r.url.path) for r in httpx_mock.get_requests()
        if r.method in ("PUT", "POST", "DELETE")
        and "/credentials/nfc_cards/import" not in r.url.path
    ]
    # Expected: deactivate(100), update_credential(101 DELETE then PUT card),
    # update_policy(102).
    assert write_path_methods == [
        ("PUT", "/api/v1/developer/users/u100"),
        ("DELETE", "/api/v1/developer/users/u101/nfc_cards/delete"),
        ("PUT", "/api/v1/developer/users/u101/nfc_cards"),
        ("PUT", "/api/v1/developer/users/u102/access_policies"),
    ]
    client.close()
```

- [ ] **Step 14.2: Write the inter-call delay test**

Append:

```python
def test_apply_inter_call_delay_invoked(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """time.sleep(0.075) is called once per write."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s)
    )
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42")]),
    )
    fetched = client.fetch_users()

    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    # token-map fetch will happen because of pre-import (even for empty bucket).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )

    client.apply(_diff(to_deactivate=[fetched[0]]))
    # One write → one sleep of 0.075.
    assert sleeps == [0.075]
    client.close()
```

- [ ] **Step 14.3: Run all tests + mypy + ruff**

```bash
uv run pytest tests/test_unifi_client.py -v
uv run mypy --strict src tests
uv run ruff check .
```

All green.

- [ ] **Step 14.4: Commit**

```bash
git add tests/test_unifi_client.py
git commit -m "Add apply-order and inter-call delay integration tests"
```

---

## Task 15: Final consistency check

**Files:**
- Run only — no edits expected.

- [ ] **Step 15.1: Full test suite**

```bash
uv run pytest -v
```

Expected: ALL tests pass (the existing CiviCRM/config/reconciler/safety/tier_mapping/models tests plus all new UniFi tests).

- [ ] **Step 15.2: Full mypy**

```bash
uv run mypy --strict src tests
```

Expected: no errors.

- [ ] **Step 15.3: Full ruff**

```bash
uv run ruff check .
```

Expected: no findings.

- [ ] **Step 15.4: Check that `pytest-httpx` is still a dev dep**

```bash
grep -A1 "pytest-httpx" pyproject.toml
```

Expected: it appears under `[dependency-groups].dev`. (Should already be there from the CiviCRM slice — confirm we didn't accidentally move it.)

- [ ] **Step 15.5: Verify the UniFi client file is reasonably sized**

```bash
wc -l src/door_sync/unifi/client.py
```

Expected: roughly 350–500 lines. If it's way larger, scan for accidental duplication; if way smaller, scan for missed bucket logic.

- [ ] **Step 15.6: Verify spec coverage**

Open `docs/superpowers/specs/2026-05-22-unifi-client-design.md` and mentally check each test in §13 has a matching test in `tests/test_unifi_client.py`. Notably:

- TLS construction tests (1-2): ✓ Task 4
- Context manager (3): ✓ Task 4
- fetch_users (4-9): ✓ Task 6
- apply dry-run (10-11): ✓ Task 7
- apply preconditions + bucket tests (12-21): ✓ Tasks 7, 10–14
- HTTP retries (23-27): ✓ Task 5/6
- nfc_id helpers (28-33): ✓ Tasks 2-3
- name split (30-33 in spec): ✓ Task 3

If you find a gap, add the test now.

- [ ] **Step 15.7: No commit needed for this task** (verification only)

---

## Notes for the implementer

- **The retry helper's loop control flow is subtle** — `continue` after sleep on retryable codes; `raise` after exhausting attempts; `return response` on success. Same shape as `civicrm/client.py:_with_retries`. Don't refactor.
- **httpx ignores `params=None` cleanly**, but be explicit — pass `params=...` only when you mean it.
- **`pytest-httpx` is strict about URL matching by default** — query string must match exactly, including order. The tests above use `?page_num=1&page_size=100&expand[]=access_policy` in that order; if `httpx` produces a different ordering, set `url=re.compile(...)` instead.
- **Don't catch `UnifiClientError` inside the client.** The orchestrator does not catch; the scheduler does (architecture §3).
- **The `_fetched_users_done` flag stays True after the first call.** A second `fetch_users()` is allowed and will refresh the caches, but `apply()` only checks that *some* prior `fetch_users()` happened.
