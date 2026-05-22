# CiviCRM Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `src/door_sync/civicrm/client.py` so `CivicrmClient(config.civicrm).fetch_active() -> list[CiviMember]` returns the door-eligible CiviCRM contacts (non-empty card_id + active membership) via two API4 calls joined in Python.

**Architecture:** Sync `httpx` client; two queries (`Contact.get` then `Membership.get`) joined in Python by contact_id. Hand-rolled retries with exponential backoff and `Retry-After` support. Tests use `pytest-httpx` to intercept httpx calls and serve canned responses. Configurable `card_id_field` per deployment.

**Tech Stack:** Python 3.11+, `uv`, sync `httpx` (existing dep), new dev dep `pytest-httpx`. No new runtime deps.

**Spec:** [`docs/superpowers/specs/2026-05-22-civicrm-client-design.md`](../specs/2026-05-22-civicrm-client-design.md).

**Conventions (architecture §11):**
- Type hints on every function. `mypy --strict src tests` must be green.
- Imports: stdlib → third-party → `door_sync.*`. No `from x import *`.
- No `sys.exit`. Errors raise `CivicrmClientError`.
- No `assert` for invariants — use explicit `if`.
- Card IDs are sensitive: never log full values (architecture §11). This client doesn't log; the audit/alert slice will redact.

**Verification commands** (used at the end of every task):

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

**Before you begin (one-time manual step, outside the plan):** The exact auth header (`Authorization: Bearer` vs `X-Civi-Auth: Bearer` vs `?api_key=`) and body format (form-encoded vs pure JSON) vary across CiviCRM deployments. Before kicking off Task 3 (where the HTTP shape becomes load-bearing), the **human driver** should make one manual `curl` or REPL call against the real prod CiviCRM endpoint to confirm the shape this plan assumes (form-encoded `params=<json>` body, `Authorization: Bearer <api_key>` header). If the deployment uses a different shape, update spec §5 + Task 3 steps to match before writing the tests. The 5 tasks below assume the documented shape.

---

## Task 1: Add `card_id_field` to `CivicrmConfig`

**Files:**
- Modify: `src/door_sync/config.py` (extend `CivicrmConfig` dataclass + `_validate_civicrm`)
- Modify: `config.example.toml` (document new field)
- Modify: `tests/test_config.py` (update helper, existing tests, add new test)

### Background

The CiviCRM client needs to know which custom field on the Contact entity holds the door access card ID. This is deployment-specific, so it goes in `CivicrmConfig`. Adding it now (before the client) means the client's constructor can read it directly with no config-side surprises.

- [ ] **Step 1.1: Update `CivicrmConfig` in `src/door_sync/config.py`**

Find the existing class:
```python
@dataclass(frozen=True)
class CivicrmConfig:
    host: str
    api_key: str
```

Replace with:
```python
@dataclass(frozen=True)
class CivicrmConfig:
    host: str
    api_key: str
    card_id_field: str
```

- [ ] **Step 1.2: Extend `_validate_civicrm` to require `card_id_field`**

Find this in `src/door_sync/config.py` (inside `_validate_civicrm`, just after the `api_key` validation, before the `return CivicrmConfig(...)`):

```python
    api_key = (env_get("CIVICRM_API_KEY") or "").strip()
    if not api_key:
        issues.append(
            ConfigIssue(
                path="CIVICRM_API_KEY",
                message="required env var is missing or empty",
            )
        )
    return CivicrmConfig(host=host, api_key=api_key)
```

Replace with:

```python
    api_key = (env_get("CIVICRM_API_KEY") or "").strip()
    if not api_key:
        issues.append(
            ConfigIssue(
                path="CIVICRM_API_KEY",
                message="required env var is missing or empty",
            )
        )
    card_id_field = section.get("card_id_field", "")
    if not isinstance(card_id_field, str) or not card_id_field.strip():
        issues.append(
            ConfigIssue(
                path="civicrm.card_id_field",
                message="must be non-empty string",
            )
        )
        card_id_field = ""
    return CivicrmConfig(
        host=host, api_key=api_key, card_id_field=card_id_field
    )
```

- [ ] **Step 1.3: Update `config.example.toml`**

Find the existing `[civicrm]` block:

```toml
[civicrm]
# Base URL for the CiviCRM site (must start with https://).
host = "https://civicrm.example.org"
```

Replace with:

```toml
[civicrm]
# Base URL for the CiviCRM site (must start with https://).
host = "https://civicrm.example.org"
# CiviCRM API4 custom field on the Contact entity holding the access card ID.
# Format: "CustomGroupName.field_name" (e.g. "Door_Access.card_id").
card_id_field = "Door_Access.card_id"
```

- [ ] **Step 1.4: Update `_write_minimal_valid` helper in `tests/test_config.py`**

Find:

```python
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
```

Replace the `[civicrm]` block to include `card_id_field`:

```python
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
    )
    env.write_text("CIVICRM_API_KEY=civikey\nUNIFI_API_KEY=unifikey\n")
    return cfg, env
```

- [ ] **Step 1.5: Update `test_load_happy_path_returns_populated_config`**

Find the existing happy-path test in `tests/test_config.py`. Add this assertion **after** `assert result.civicrm.api_key == "civikey"`:

```python
    assert result.civicrm.card_id_field == "Door_Access.card_id"
```

- [ ] **Step 1.6: Update `test_example_files_parse` (drift test)**

Find the existing drift test in `tests/test_config.py`. Add this assertion alongside the other civicrm assertions:

```python
    assert result.civicrm.card_id_field == "Door_Access.card_id"
```

- [ ] **Step 1.7: Add a new test for missing `card_id_field`**

Add this test **after** `test_baseline_floor_validation_rejects_negative` (or anywhere in the validator-test section):

```python
def test_civicrm_missing_card_id_field_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """card_id_field is required; missing or empty fails validation."""
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
```

- [ ] **Step 1.8: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 118 tests pass (117 prior + 1 new); mypy success; ruff clean.

If anything fails, the most likely cause is a missed update to a test that builds a `CivicrmConfig` directly without `card_id_field`. Search `tests/` for `CivicrmConfig(` to find any stragglers.

- [ ] **Step 1.9: Commit**

```bash
git add src/door_sync/config.py config.example.toml tests/test_config.py
git commit -m "Add card_id_field to CivicrmConfig"
```

---

## Task 2: Add `pytest-httpx` + create `civicrm` package skeleton

**Files:**
- Modify: `pyproject.toml` (add dev dep)
- Create: `src/door_sync/civicrm/__init__.py`
- Create: `src/door_sync/civicrm/client.py`
- Create: `tests/test_civicrm_client.py`

### Background

This task scaffolds the package. The `CivicrmClient` class is built with `__init__`, `close`, `__enter__`, `__exit__`, plus a `fetch_active` stub that raises `NotImplementedError`. One test verifies the context-manager protocol closes the underlying httpx client.

`pytest-httpx` provides the `httpx_mock` fixture used in Tasks 3–5.

- [ ] **Step 2.1: Add `pytest-httpx` to dev deps in `pyproject.toml`**

Find this block in `pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "mypy>=2.1.0",
    "pytest>=9.0.3",
    "ruff>=0.15.13",
]
```

Replace with:

```toml
[dependency-groups]
dev = [
    "mypy>=2.1.0",
    "pytest>=9.0.3",
    "pytest-httpx>=0.30",
    "ruff>=0.15.13",
]
```

- [ ] **Step 2.2: Run `uv sync` to install the new dep**

```bash
uv sync
```

Expected output includes `+ pytest-httpx==<version>`. Confirm it installed without errors.

- [ ] **Step 2.3: Create `src/door_sync/civicrm/__init__.py`**

The file is empty (just makes the directory a Python package). Create it with:

```python
```

(an empty file — no content.)

- [ ] **Step 2.4: Write `src/door_sync/civicrm/client.py` (skeleton only)**

```python
"""CiviCRM API4 client for door-sync.

Reads active members (contacts with a non-empty card_id and an active
membership) from a WordPress-hosted CiviCRM instance. Read-only — no write
operations. Hand-rolled retry on 5xx and 429.

This module is not pure (it does HTTP), but it does NOT call sys.exit.
Errors surface as CivicrmClientError so the scheduler's per-cycle try/except
can log and continue.
"""

from types import TracebackType

import httpx

from door_sync.config import CivicrmConfig
from door_sync.models import CiviMember


_API_PATH = "/wp-json/civicrm/v3/api4"


class CivicrmClientError(Exception):
    """Raised on non-recoverable CiviCRM API failure."""


class CivicrmClient:
    """Read-only CiviCRM API4 client.

    Construct one per reconcile cycle; the underlying httpx.Client owns
    connection state. Use as a context manager, or call close() explicitly.
    """

    def __init__(self, config: CivicrmConfig) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.host,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            verify=True,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    def fetch_active(self) -> list[CiviMember]:
        raise NotImplementedError("implemented in Task 3")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CivicrmClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
```

- [ ] **Step 2.5: Write `tests/test_civicrm_client.py` (initial version)**

```python
"""Tests for the CiviCRM API4 client."""

import pytest

from door_sync.civicrm.client import CivicrmClient
from door_sync.config import CivicrmConfig


def _config() -> CivicrmConfig:
    return CivicrmConfig(
        host="https://civi.example.org",
        api_key="testkey",
        card_id_field="Door_Access.card_id",
    )


def test_context_manager_closes_http_client() -> None:
    """Using CivicrmClient as a context manager closes the underlying httpx.Client."""
    with CivicrmClient(_config()) as client:
        assert client._http.is_closed is False
    assert client._http.is_closed is True


def test_fetch_active_raises_not_implemented_for_now() -> None:
    """Sanity: skeleton-only client raises NotImplementedError. Replaced in Task 3."""
    with CivicrmClient(_config()) as client:
        with pytest.raises(NotImplementedError):
            client.fetch_active()
```

The `client._http` access uses the private attribute. That's fine in a test of internal lifecycle. The second test (`raises NotImplementedError`) will be **deleted in Task 3** when we implement the method.

- [ ] **Step 2.6: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 2 new tests pass (120 total); mypy success; ruff clean.

If mypy complains about `client._http` access in the test (`--strict` may flag private attribute access), add `# type: ignore[reportPrivateUsage]` or restructure to use a property. The simplest fix: leave it; mypy doesn't enforce private-name access by default. If ruff complains about the unused `pytest` import in the test file — it's used by the second test's `pytest.raises`, so it should be fine.

- [ ] **Step 2.7: Commit**

```bash
git add pyproject.toml uv.lock src/door_sync/civicrm/ tests/test_civicrm_client.py
git commit -m "Add CivicrmClient skeleton and pytest-httpx dev dep"
```

---

## Task 3: Implement `fetch_active` happy path (no pagination, no retries)

**Files:**
- Modify: `src/door_sync/civicrm/client.py` (replace `fetch_active` stub)
- Modify: `tests/test_civicrm_client.py` (delete the NotImplementedError test, add 5 happy-path tests)

### Background

`fetch_active` runs two API4 calls and joins them in Python:

1. `Contact.get` → contacts with non-empty `card_id_field` and `is_deleted = false`
2. `Membership.get` → memberships filtered to active statuses (`Current`, `Grace`) for those contacts
3. Join by `contact_id` in Python; build `list[CiviMember]`

This task implements the **single-page** version. Pagination is added in Task 4. The HTTP layer is also **bare** — no retries. Task 5 adds retries.

The HTTP shape (request body, auth) follows spec §5. If the manual prod-verification step at the top of the plan revealed a different shape, adjust the `_post` helper accordingly.

- [ ] **Step 3.1: Replace `fetch_active` and add private helpers in `src/door_sync/civicrm/client.py`**

Update the imports at the top of `src/door_sync/civicrm/client.py` to include `json` and `Any`:

```python
"""CiviCRM API4 client for door-sync.

Reads active members (contacts with a non-empty card_id and an active
membership) from a WordPress-hosted CiviCRM instance. Read-only — no write
operations. Hand-rolled retry on 5xx and 429.

This module is not pure (it does HTTP), but it does NOT call sys.exit.
Errors surface as CivicrmClientError so the scheduler's per-cycle try/except
can log and continue.
"""

import json
from types import TracebackType
from typing import Any

import httpx

from door_sync.config import CivicrmConfig
from door_sync.models import CiviMember


_API_PATH = "/wp-json/civicrm/v3/api4"
_PAGE_SIZE = 250
_ACTIVE_STATUSES = ["Current", "Grace"]
```

Replace the `fetch_active` stub with:

```python
    def fetch_active(self) -> list[CiviMember]:
        contacts = self._fetch_contacts()
        if not contacts:
            return []
        contact_ids = [int(c["id"]) for c in contacts]
        memberships = self._fetch_memberships(contact_ids)

        types_by_contact: dict[int, list[str]] = {}
        for m in memberships:
            cid = int(m["contact_id"])
            label = str(m["membership_type_id:label"])
            types_by_contact.setdefault(cid, []).append(label)

        return [
            CiviMember(
                contact_id=int(c["id"]),
                display_name=str(c["display_name"]),
                card_id=_coerce_card_id(c.get(self._config.card_id_field)),
                membership_types=types_by_contact.get(int(c["id"]), []),
            )
            for c in contacts
        ]

    def _fetch_contacts(self) -> list[dict[str, Any]]:
        return self._post(
            "Contact",
            "get",
            {
                "select": ["id", "display_name", self._config.card_id_field],
                "where": [
                    [self._config.card_id_field, "IS NOT EMPTY"],
                    ["is_deleted", "=", False],
                ],
                "limit": _PAGE_SIZE,
            },
        )

    def _fetch_memberships(self, contact_ids: list[int]) -> list[dict[str, Any]]:
        return self._post(
            "Membership",
            "get",
            {
                "select": [
                    "contact_id",
                    "membership_type_id:label",
                    "status_id:name",
                ],
                "where": [
                    ["contact_id", "IN", contact_ids],
                    ["status_id:name", "IN", _ACTIVE_STATUSES],
                ],
                "limit": _PAGE_SIZE,
            },
        )

    def _post(
        self, entity: str, action: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        url = f"{_API_PATH}/{entity}/{action}"
        data = {"params": json.dumps(params)}
        response = self._http.post(url, data=data)
        payload = response.json()
        values = payload.get("values", [])
        if not isinstance(values, list):
            return []
        return values
```

Add this private helper at the **bottom** of the file (after the class):

```python
def _coerce_card_id(raw: object) -> int | None:
    """CiviCRM may return card_id as int or string depending on the field type.

    Empty string and None map to None; everything else is parsed as int.
    Caller has already filtered contacts to non-empty card_id, so the None
    path is defensive only.
    """
    if raw is None or raw == "":
        return None
    return int(raw)  # type: ignore[arg-type]
```

(The `# type: ignore[arg-type]` is because `int()` accepts `int | str | bytes | ...` but `raw` is typed `object`. The runtime check above guarantees `raw` is something `int()` accepts, but mypy can't see that. The narrow ignore is preferable to a broader cast.)

- [ ] **Step 3.2: Update `tests/test_civicrm_client.py`**

Replace the **entire file** with:

```python
"""Tests for the CiviCRM API4 client."""

import json
import urllib.parse
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from door_sync.civicrm.client import CivicrmClient
from door_sync.config import CivicrmConfig


def _config() -> CivicrmConfig:
    return CivicrmConfig(
        host="https://civi.example.org",
        api_key="testkey",
        card_id_field="Door_Access.card_id",
    )


def _contact(
    contact_id: int,
    display_name: str = "Test Person",
    card_id: int | str = 100,
) -> dict[str, Any]:
    return {
        "id": contact_id,
        "display_name": display_name,
        "Door_Access.card_id": card_id,
    }


def _membership(
    contact_id: int,
    type_label: str = "Gold",
    status_name: str = "Current",
) -> dict[str, Any]:
    return {
        "contact_id": contact_id,
        "membership_type_id:label": type_label,
        "status_id:name": status_name,
    }


def _values_response(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {"values": values, "count": len(values)}


def _register_contacts(
    httpx_mock: HTTPXMock,
    values: list[dict[str, Any]],
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        json=_values_response(values),
    )


def _register_memberships(
    httpx_mock: HTTPXMock,
    values: list[dict[str, Any]],
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response(values),
    )


# --- Lifecycle ---


def test_context_manager_closes_http_client() -> None:
    """Using CivicrmClient as a context manager closes the underlying httpx.Client."""
    with CivicrmClient(_config()) as client:
        assert client._http.is_closed is False
    assert client._http.is_closed is True


# --- fetch_active happy paths ---


def test_fetch_active_happy_path(httpx_mock: HTTPXMock) -> None:
    """One contact, one Current membership → one CiviMember with the right fields."""
    _register_contacts(httpx_mock, [_contact(42, "Jane Doe", card_id=12345)])
    _register_memberships(httpx_mock, [_membership(42, "Gold", "Current")])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    member = result[0]
    assert member.contact_id == 42
    assert member.display_name == "Jane Doe"
    assert member.card_id == 12345
    assert member.membership_types == ["Gold"]


def test_fetch_active_empty_result(httpx_mock: HTTPXMock) -> None:
    """Zero contacts → returns []. The memberships query is NOT made."""
    _register_contacts(httpx_mock, [])
    # NOTE: no membership response registered — if the client tried to call it,
    # pytest-httpx would raise an unregistered-request error.

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result == []


def test_contact_with_multiple_active_memberships(httpx_mock: HTTPXMock) -> None:
    """Contact with both Gold (Current) and Comp (Current) → membership_types has both."""
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(
        httpx_mock,
        [
            _membership(42, "Gold", "Current"),
            _membership(42, "Comp", "Current"),
        ],
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert sorted(result[0].membership_types) == ["Comp", "Gold"]


def test_contact_with_no_active_membership_kept_with_empty_types(
    httpx_mock: HTTPXMock,
) -> None:
    """Contact has card_id but no Current/Grace memberships → empty list, NOT excluded.

    This member resolves to "unmapped" in tier_mapping, which the safety guard
    halts on — surfacing the data issue rather than silently ignoring.
    """
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert result[0].contact_id == 42
    assert result[0].membership_types == []


def test_expired_memberships_filtered(httpx_mock: HTTPXMock) -> None:
    """Server-side where filter only returns Current/Grace.

    This test verifies the client passes the correct filter; it doesn't test
    CiviCRM's filtering behavior. We register only what the server would return
    (i.e., already filtered), and assert the membership_types reflect that.
    """
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(
        httpx_mock,
        [
            # Server returned only the Grace one because of our where clause
            _membership(42, "Silver", "Grace"),
        ],
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result[0].membership_types == ["Silver"]


def test_request_uses_bearer_auth_and_form_body(httpx_mock: HTTPXMock) -> None:
    """Sanity-check the HTTP shape (auth header + form body) against spec §5."""
    _register_contacts(httpx_mock, [])

    with CivicrmClient(_config()) as client:
        client.fetch_active()

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    req = requests[0]
    assert req.headers["authorization"] == "Bearer testkey"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    # Body is form-encoded `params=<json>`. Decode and verify the JSON shape.
    body = req.content.decode()
    parsed = urllib.parse.parse_qs(body)
    params = json.loads(parsed["params"][0])
    assert "select" in params
    assert params["select"] == ["id", "display_name", "Door_Access.card_id"]
```

- [ ] **Step 3.3: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 7 new tests pass (1 lifecycle + 6 fetch_active); mypy success; ruff clean.

If `mypy` complains about `client._http.is_closed` (accessing private attribute) — that's a quirk of strict mode. Suppress narrowly:

```python
assert client._http.is_closed is False  # type: ignore[attr-defined]
```

If `mypy` complains about `payload.get("values", [])` (calling `.get` on `Any`), the `isinstance(values, list)` check in `_post` should narrow it. If not, suppress with `# type: ignore[no-any-return]` on the return.

- [ ] **Step 3.4: Commit**

```bash
git add src/door_sync/civicrm/client.py tests/test_civicrm_client.py
git commit -m "Implement CivicrmClient.fetch_active happy path"
```

---

## Task 4: Pagination

**Files:**
- Modify: `src/door_sync/civicrm/client.py` (add loops to `_fetch_contacts` and `_fetch_memberships`)
- Modify: `tests/test_civicrm_client.py` (add 2 pagination tests)

### Background

CiviCRM API4 supports `limit` and `offset`. The client paginates at `_PAGE_SIZE` (250) per page and follows until a short page is returned. The loops live inside the two `_fetch_*` helpers; `_post` stays simple.

- [ ] **Step 4.1: Write the failing pagination tests in `tests/test_civicrm_client.py`**

Add these tests **after** `test_request_uses_bearer_auth_and_form_body`:

```python
# --- Pagination ---


def test_fetch_active_paginates_contacts(httpx_mock: HTTPXMock) -> None:
    """251 contacts arrive as a full page of 250 then a short page of 1."""
    full_page = [_contact(i) for i in range(1, 251)]
    short_page = [_contact(251)]

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        json=_values_response(full_page),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        json=_values_response(short_page),
    )
    # All 251 contacts have Gold/Current memberships, returned in one page
    _register_memberships(
        httpx_mock,
        [_membership(i, "Gold", "Current") for i in range(1, 252)],
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 251
    assert {m.contact_id for m in result} == set(range(1, 252))

    # Confirm two Contact.get requests were made with offset=0 and offset=250
    contact_requests = [
        r for r in httpx_mock.get_requests()
        if "/Contact/get" in str(r.url)
    ]
    assert len(contact_requests) == 2
    offsets = sorted(
        json.loads(
            urllib.parse.parse_qs(r.content.decode())["params"][0]
        ).get("offset", 0)
        for r in contact_requests
    )
    assert offsets == [0, 250]


def test_fetch_active_paginates_memberships(httpx_mock: HTTPXMock) -> None:
    """251 memberships across 2 pages."""
    _register_contacts(httpx_mock, [_contact(1)])

    full_page = [_membership(1, f"Type{i}", "Current") for i in range(250)]
    short_page = [_membership(1, "Type250", "Current")]
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response(full_page),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response(short_page),
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert len(result[0].membership_types) == 251
```

- [ ] **Step 4.2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_civicrm_client.py -v -k "paginates"
```

Expected: both tests fail because the current `_fetch_contacts` and `_fetch_memberships` only call `_post` once. Failure mode is most likely "unregistered request" (pytest-httpx complains about the second registered response not being consumed) or `assert len(result) == 251` failing because only 250 came back.

- [ ] **Step 4.3: Add pagination loops to `_fetch_contacts` and `_fetch_memberships`**

In `src/door_sync/civicrm/client.py`, replace `_fetch_contacts`:

```python
    def _fetch_contacts(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._post(
                "Contact",
                "get",
                {
                    "select": ["id", "display_name", self._config.card_id_field],
                    "where": [
                        [self._config.card_id_field, "IS NOT EMPTY"],
                        ["is_deleted", "=", False],
                    ],
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                },
            )
            results.extend(page)
            if len(page) < _PAGE_SIZE:
                return results
            offset += _PAGE_SIZE
```

Replace `_fetch_memberships`:

```python
    def _fetch_memberships(self, contact_ids: list[int]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._post(
                "Membership",
                "get",
                {
                    "select": [
                        "contact_id",
                        "membership_type_id:label",
                        "status_id:name",
                    ],
                    "where": [
                        ["contact_id", "IN", contact_ids],
                        ["status_id:name", "IN", _ACTIVE_STATUSES],
                    ],
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                },
            )
            results.extend(page)
            if len(page) < _PAGE_SIZE:
                return results
            offset += _PAGE_SIZE
```

- [ ] **Step 4.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 9 tests in `test_civicrm_client.py` (1 lifecycle + 6 happy + 2 pagination); mypy success; ruff clean.

- [ ] **Step 4.5: Commit**

```bash
git add src/door_sync/civicrm/client.py tests/test_civicrm_client.py
git commit -m "Add pagination to CivicrmClient fetches"
```

---

## Task 5: Retries + error paths

**Files:**
- Modify: `src/door_sync/civicrm/client.py` (add `_with_retries`, `_backoff_seconds`, `_parse_retry_after`; wrap HTTP call in `_post`; handle malformed JSON)
- Modify: `tests/test_civicrm_client.py` (add 5 error-path tests)

### Background

Per spec §7: 3 attempts, exponential backoff with ±20% jitter, retry on 5xx + 429 + network errors, honor `Retry-After: <seconds>` header on 429, raise `CivicrmClientError` after exhaustion. No retry on 4xx (other than 429).

`_post` wraps the HTTP call in `_with_retries`, and also catches malformed JSON to raise `CivicrmClientError`.

`time.sleep` is patched in tests via `monkeypatch.setattr("time.sleep", recorder)` to keep tests fast and let us assert the wait was respected.

- [ ] **Step 5.1: Write the failing error-path tests in `tests/test_civicrm_client.py`**

First, add `CivicrmClientError` to the import from `door_sync.civicrm.client`. The import line becomes:

```python
from door_sync.civicrm.client import CivicrmClient, CivicrmClientError
```

(No `import time` needed — `monkeypatch.setattr("time.sleep", ...)` patches by string reference.)

Then add these tests **after** the pagination tests:

```python
# --- Retries and error paths ---


def test_http_401_raises_no_retry(httpx_mock: HTTPXMock) -> None:
    """401 is a permanent auth error — no retry, raise CivicrmClientError."""
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=401,
        text="Unauthorized",
    )

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="401"):
            client.fetch_active()

    assert len(httpx_mock.get_requests()) == 1  # No retries


def test_http_500_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three 500 responses → raise CivicrmClientError; three requests made."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
            status_code=500,
            text="Internal Server Error",
        )

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="500"):
            client.fetch_active()

    assert len(httpx_mock.get_requests()) == 3
    assert len(sleep_calls) == 2  # Sleep between attempts, not after the last


def test_http_500_then_200_succeeds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 then 200 → success after one retry."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=500,
    )
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [_membership(42)])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert len(sleep_calls) == 1


def test_http_429_honors_retry_after_seconds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 with Retry-After: 5 → client waits at least 5s before retrying."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=429,
        headers={"Retry-After": "5"},
    )
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [_membership(42)])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert any(s >= 5 for s in sleep_calls), f"Expected a sleep >= 5s, got {sleep_calls}"


def test_malformed_json_raises(httpx_mock: HTTPXMock) -> None:
    """200 with invalid JSON body → CivicrmClientError."""
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=200,
        text="not valid json {",
    )

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="malformed"):
            client.fetch_active()
```

- [ ] **Step 5.2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_civicrm_client.py -v -k "401 or 500 or 429 or malformed"
```

Expected: all 5 fail. The 401 test will hit an unhandled httpx response (`raise_for_status` not called, but `payload.get(...)` will return `[]` if json parse succeeds, or raise on parse failure). The 500 tests will see only one request. The malformed test will fail with a `json.JSONDecodeError` not wrapped.

- [ ] **Step 5.3: Add retry helpers and update `_post` in `src/door_sync/civicrm/client.py`**

Update the imports at the top of `src/door_sync/civicrm/client.py` to add `random` and `time` and `Callable`:

```python
import json
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

import httpx

from door_sync.config import CivicrmConfig
from door_sync.models import CiviMember


_API_PATH = "/wp-json/civicrm/v3/api4"
_PAGE_SIZE = 250
_ACTIVE_STATUSES = ["Current", "Grace"]
_MAX_ATTEMPTS = 3
```

Replace `_post` (currently the no-retry version):

```python
    def _post(
        self, entity: str, action: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        url = f"{_API_PATH}/{entity}/{action}"
        data = {"params": json.dumps(params)}
        response = self._with_retries(lambda: self._http.post(url, data=data))
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise CivicrmClientError(
                f"malformed JSON response from {url}: {e}"
            ) from e
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return []
        return values

    def _with_retries(
        self, action: Callable[[], httpx.Response]
    ) -> httpx.Response:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = action()
            except httpx.RequestError as e:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(
                        f"network failure after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code == 429:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(
                        f"HTTP 429 (rate limited) after {_MAX_ATTEMPTS} attempts"
                    )
                wait = _parse_retry_after(response) or _backoff_seconds(attempt)
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(
                        f"HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts: "
                        f"{response.text[:200]}"
                    )
                time.sleep(_backoff_seconds(attempt))
                continue

            if response.status_code >= 400:
                # 4xx other than 429 → permanent, no retry
                raise CivicrmClientError(
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )

            return response

        # Unreachable (mypy/typing only); the loop always returns or raises.
        raise CivicrmClientError("retry loop exited unexpectedly")
```

Add these module-level helpers at the **bottom** of the file (next to `_coerce_card_id`):

```python
def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with ±20% jitter. attempt is 1-indexed."""
    base = float(2 ** (attempt - 1))  # 1.0, 2.0, 4.0 ...
    jitter = random.uniform(-0.2, 0.2) * base
    return max(0.1, base + jitter)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header. Returns the seconds value if it's a number.

    HTTP-date form is not supported (per spec §13) and returns None.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
```

- [ ] **Step 5.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 14 tests in `test_civicrm_client.py` (1 lifecycle + 6 happy + 2 pagination + 5 error); mypy success; ruff clean.

If `mypy` complains about the unreachable `raise CivicrmClientError("retry loop exited unexpectedly")` — that's expected (mypy sees that all paths in the loop either return or raise but can't prove the loop always executes). The line is there to satisfy the type checker. If mypy is happy without it, remove it.

If `ruff` complains about `B904` (raise without `from`) on the unreachable line, leave it as-is — there's no exception to chain from. If ruff is unhappy specifically about that line, change it to `raise CivicrmClientError("retry loop exited unexpectedly") from None`.

If `ruff` complains about `random.uniform` (PRNG seed concerns under `S311` / `S102`), it shouldn't — those checks are not in the default ruff config. If they are, add `# noqa: S311` or configure `[tool.ruff.lint] ignore = ["S311"]` — but only if the rule is actually firing.

- [ ] **Step 5.5: Commit**

```bash
git add src/door_sync/civicrm/client.py tests/test_civicrm_client.py
git commit -m "Add retries and error handling to CivicrmClient"
```

---

## Final verification

After Task 5 is committed, do one more pass against the spec's Definition of Done (§2):

- [ ] **Step F.1: All three checks green from scratch**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: ~131 tests total (117 from prior slices + 14 new for civicrm); mypy + ruff clean.

- [ ] **Step F.2: All 13 spec-§11 tests present**

```bash
uv run pytest tests/test_civicrm_client.py -v --collect-only | grep "test_"
```

Verify these test names appear (the spec asked for 12+ tests; the plan delivers 14 including the auth/body shape sanity test):

- `test_context_manager_closes_http_client`
- `test_fetch_active_happy_path`
- `test_fetch_active_empty_result`
- `test_contact_with_multiple_active_memberships`
- `test_contact_with_no_active_membership_kept_with_empty_types`
- `test_expired_memberships_filtered`
- `test_request_uses_bearer_auth_and_form_body`
- `test_fetch_active_paginates_contacts`
- `test_fetch_active_paginates_memberships`
- `test_http_401_raises_no_retry`
- `test_http_500_retries_then_raises`
- `test_http_500_then_200_succeeds`
- `test_http_429_honors_retry_after_seconds`
- `test_malformed_json_raises`

- [ ] **Step F.3: Pure-stdlib + httpx, no other runtime deps**

```bash
grep -nE "^(import|from)" src/door_sync/civicrm/client.py
```

Expected: stdlib (`json`, `random`, `time`, `types`, `typing`, `collections.abc`), `httpx`, and `door_sync.*`. No other third-party.

- [ ] **Step F.4: Client never calls `sys.exit`**

```bash
grep -n "sys.exit\|SystemExit" src/door_sync/civicrm/client.py
```

Expected: no matches (or only the docstring mention).

- [ ] **Step F.5: Commit history is clean**

```bash
git log --oneline ae5e9d2..HEAD
```

Expected: 5 task commits in order.

If any check fails, fix and add a follow-up commit — do not mark the slice done.
