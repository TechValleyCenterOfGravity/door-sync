# Sync Member Email (CiviCRM → UniFi) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision and keep a member's CiviCRM primary email synced onto the UniFi Access `user_email` field, so credential/PIN invites reach the right address.

**Architecture:** Add one optional `email` field that rides the existing `CiviMember → ResolvedMember → Diff → UnifiUser` flow. Email reconciles inside the existing `to_update_credential` set (shared `PUT /users/{id}` endpoint), compared case-insensitively. No new module, no new diff set, pure/impure boundary and safety guards untouched. Members resolving to a door tier with no email are provisioned anyway, with one WARN logged in the orchestrator.

**Tech Stack:** Python 3, frozen dataclasses, sync `httpx`, `pytest` + `pytest-httpx`, `uv` for all tooling, `pyrefly` type checking, `ruff` lint/format.

**Key design decision — default `None`:** The new `email` field is added with a default of `= None` on all three dataclasses. This is the only defaulted field on each, appended last, so dataclass ordering stays valid. Because writes are conditional (`if email is not None` / case-insensitive compare), every existing strict-equality test on request bodies keeps passing unchanged — the new field only affects behavior when an email is actually present. Email-`None` is a legitimate domain value (warn-but-provision), so a default is semantically correct, not a shortcut.

Full design: `docs/superpowers/specs/2026-06-17-sync-member-email-design.md`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/door_sync/models.py` | Domain dataclasses | Add `email: str \| None = None` to `CiviMember`, `ResolvedMember`, `UnifiUser` |
| `src/door_sync/tier_mapping.py` | Pure resolution | Pass `email` through (all `ResolvedMember(...)` sites) |
| `src/door_sync/reconciler.py` | Pure diff | Extend `cred_changed` with case-insensitive email compare; add private `_norm`/`_email_differs` |
| `src/door_sync/civicrm/client.py` | Read CiviCRM | Add `email_primary.email` to select; map onto `CiviMember.email` |
| `src/door_sync/unifi/client.py` | Read+write UniFi | Parse `user_email` on read; write it on create/reactivate/update; local `_email_differs_ci`; dry-run log line |
| `src/door_sync/orchestrator.py` | Wiring + logging | WARN per `tier`-resolved member with no email |
| `docs/architecture.md` | Data contracts reference | Update §6 dataclass definitions to include `email` |
| `tests/test_models.py` | Model tests | New: email field + default |
| `tests/test_tier_mapping.py` | Pure tests | Email passthrough |
| `tests/test_reconciler.py` | Pure tests | Email diff + idempotency |
| `tests/test_civicrm_client.py` | Client tests | Email select + mapping; update select-assertion |
| `tests/test_unifi_client.py` | Client tests | Email read + write bodies |
| `tests/test_orchestrator.py` | Integration | Missing-email WARN |

---

## Task 1: Add `email` field to domain models

**Files:**
- Modify: `src/door_sync/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_civi_member_email_defaults_none_and_accepts_value() -> None:
    from door_sync.models import CiviMember

    m = CiviMember(contact_id=1, display_name="A", card_id=10, membership_types=("Gold",))
    assert m.email is None

    m2 = CiviMember(
        contact_id=1,
        display_name="A",
        card_id=10,
        membership_types=("Gold",),
        email="a@example.com",
    )
    assert m2.email == "a@example.com"


def test_resolved_member_email_defaults_none_and_accepts_value() -> None:
    from door_sync.models import ResolvedMember

    r = ResolvedMember(
        contact_id=1, display_name="A", card_id=10, target_policy="P", resolution="tier"
    )
    assert r.email is None
    r2 = ResolvedMember(
        contact_id=1,
        display_name="A",
        card_id=10,
        target_policy="P",
        resolution="tier",
        email="a@example.com",
    )
    assert r2.email == "a@example.com"


def test_unifi_user_email_defaults_none_and_accepts_value() -> None:
    from door_sync.models import UnifiUser

    u = UnifiUser(contact_id=1, display_name="A", card_id=10, active=True, policy="P")
    assert u.email is None
    u2 = UnifiUser(
        contact_id=1, display_name="A", card_id=10, active=True, policy="P", email="a@example.com"
    )
    assert u2.email == "a@example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -k email -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'email'`

- [ ] **Step 3: Implement the field on all three dataclasses**

In `src/door_sync/models.py`, add `email` as the last field of `CiviMember`, `ResolvedMember`, and `UnifiUser`, and document it.

`CiviMember` — after `membership_types`:

```python
    membership_types: tuple[str, ...]
    email: str | None = None
```

Update its docstring `Parameters:` block, adding:

```text
        email: Contact's primary email from CiviCRM, or None if absent.
```

`ResolvedMember` — after `resolution`:

```python
    resolution: Literal["tier", "none", "day-pass", "unmapped"]
    email: str | None = None
```

Add to its docstring:

```text
        email: Contact's primary email carried through from CiviCRM, or None.
```

`UnifiUser` — after `policy`:

```python
    policy: str | None
    email: str | None = None
```

Add to its docstring:

```text
        email: User's email (user_email) as read from UniFi, or None.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS (all model tests, including the three new ones)

- [ ] **Step 5: Commit**

```bash
git add src/door_sync/models.py tests/test_models.py
git commit -m "feat(models): add optional email field to CiviMember, ResolvedMember, UnifiUser"
```

---

## Task 2: Pass email through tier mapping (pure)

**Files:**
- Modify: `src/door_sync/tier_mapping.py`
- Test: `tests/test_tier_mapping.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_tier_mapping.py`, update the `_civi` helper to accept an email, then add passthrough tests. Replace the existing `_civi` helper:

```python
def _civi(types: tuple[str, ...], email: str | None = "m@example.com") -> CiviMember:
    return CiviMember(
        contact_id=1, display_name="A", card_id=42, membership_types=types, email=email
    )
```

Add these tests at the end of the file:

```python
def test_email_passes_through_on_tier_match() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(("Gold",), email="gold@example.com"), mapping)
    assert result.email == "gold@example.com"


def test_email_passes_through_on_none_resolution() -> None:
    mapping = TierMapping(rules={"Comp": TierRule(resolution="none", target_policy=None, rank=1)})
    result = resolve(_civi(("Comp",), email="comp@example.com"), mapping)
    assert result.email == "comp@example.com"


def test_email_passes_through_on_no_memberships() -> None:
    result = resolve(_civi((), email="empty@example.com"), TierMapping(rules={}))
    assert result.resolution == "none"
    assert result.email == "empty@example.com"


def test_email_passes_through_on_unmapped() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(("Silver",), email="silver@example.com"), mapping)
    assert result.resolution == "unmapped"
    assert result.email == "silver@example.com"


def test_email_none_passes_through() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(("Gold",), email=None), mapping)
    assert result.email is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tier_mapping.py -k email -v`
Expected: FAIL — `assert None == 'gold@example.com'` (email not carried through yet)

- [ ] **Step 3: Implement passthrough**

In `src/door_sync/tier_mapping.py`, add `email=member.email` to **every** `ResolvedMember(...)` construction in `resolve` (there are three: the no-memberships branch, the unmapped branch, and the final matched branch). Example for the final return:

```python
    return ResolvedMember(
        contact_id=member.contact_id,
        display_name=member.display_name,
        card_id=member.card_id,
        target_policy=chosen.target_policy,
        resolution=chosen.resolution,
        email=member.email,
    )
```

Apply the same `email=member.email` addition to the no-memberships `return` (the one with `resolution="none"`) and the unmapped `return` (the one with `resolution="unmapped"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tier_mapping.py -v`
Expected: PASS (all tier-mapping tests; existing ones unaffected because `_civi`'s email default doesn't change resolution logic)

- [ ] **Step 5: Commit**

```bash
git add src/door_sync/tier_mapping.py tests/test_tier_mapping.py
git commit -m "feat(tier_mapping): carry email through resolution"
```

---

## Task 3: Reconcile email changes via to_update_credential (pure)

**Files:**
- Modify: `src/door_sync/reconciler.py`
- Test: `tests/test_reconciler.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_reconciler.py`, update the `_resolved` and `_unifi` helpers to accept email, then add diff tests. Update the helpers:

```python
def _resolved(
    contact_id: int = 1,
    display_name: str = "Alice",
    card_id: int | None = 100,
    target_policy: str | None = "P_GOLD",
    resolution: Literal["tier", "none", "day-pass", "unmapped"] = "tier",
    email: str | None = "alice@example.com",
) -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=display_name,
        card_id=card_id,
        target_policy=target_policy,
        resolution=resolution,
        email=email,
    )


def _unifi(
    contact_id: int = 1,
    display_name: str = "Alice",
    card_id: int | None = 100,
    active: bool = True,
    policy: str | None = "P_GOLD",
    email: str | None = "alice@example.com",
) -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=display_name,
        card_id=card_id,
        active=active,
        policy=policy,
        email=email,
    )
```

Add these tests after `test_tier_display_name_differs_updates_credential`:

```python
def test_tier_email_differs_updates_credential() -> None:
    r = _resolved(email="new@example.com")
    u = _unifi(email="old@example.com")
    d = compute_diff([r], [u])
    assert d.to_update_credential == ((r, u),)
    assert d.to_update_policy == ()
    assert d.to_add == ()
    assert d.to_deactivate == ()


def test_tier_email_case_only_difference_is_noop() -> None:
    r = _resolved(email="Alice@Example.com")
    u = _unifi(email="alice@example.com")
    d = compute_diff([r], [u])
    assert d.to_update_credential == ()


def test_tier_email_none_vs_empty_is_noop() -> None:
    r = _resolved(email=None)
    u = _unifi(email="")
    d = compute_diff([r], [u])
    assert d.to_update_credential == ()


def test_tier_email_set_vs_none_updates_credential() -> None:
    r = _resolved(email="alice@example.com")
    u = _unifi(email=None)
    d = compute_diff([r], [u])
    assert d.to_update_credential == ((r, u),)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reconciler.py -k email -v`
Expected: FAIL — `test_tier_email_differs_updates_credential` fails (`assert () == ((r, u),)`) because email isn't compared yet.

- [ ] **Step 3: Implement the email compare**

In `src/door_sync/reconciler.py`, add two module-private pure helpers above `compute_diff`:

```python
def _norm_email(email: str | None) -> str | None:
    """Normalize an email for comparison: empty string and None collapse to None,
    everything else lowercases. Pure; no I/O."""
    return email.lower() if email else None


def _email_differs(a: str | None, b: str | None) -> bool:
    """Case-insensitive email comparison; None and '' are treated as equal."""
    return _norm_email(a) != _norm_email(b)
```

Then extend the `cred_changed` line inside `compute_diff` (the `u present + active + tier resolution` block):

```python
        cred_changed = (
            u.card_id != r.card_id
            or u.display_name != r.display_name
            or _email_differs(r.email, u.email)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reconciler.py -k email -v`
Expected: PASS (all four new email tests)

- [ ] **Step 5: Update the idempotency canary to carry email**

The in-memory `apply_diff_in_memory` helper in `tests/test_reconciler.py` reconstructs `UnifiUser` on add and credential-update; it must propagate email so a re-diff stays empty. In the `for r in diff.to_add:` block, add `email=r.email` to the `UnifiUser(...)`:

```python
    for r in diff.to_add:
        by_id[r.contact_id] = UnifiUser(
            contact_id=r.contact_id,
            display_name=r.display_name,
            card_id=r.card_id,
            active=True,
            policy=r.target_policy,
            email=r.email,
        )
```

In the `for r, u in diff.to_update_credential:` block, add `email=r.email`:

```python
    for r, u in diff.to_update_credential:
        existing = by_id[u.contact_id]
        by_id[u.contact_id] = UnifiUser(
            contact_id=existing.contact_id,
            display_name=r.display_name,
            card_id=r.card_id,
            active=existing.active,
            policy=existing.policy,
            email=r.email,
        )
```

(The `to_update_policy` and `to_deactivate` blocks preserve `existing` fields — leave them; email is unchanged on those paths. Because `existing` is a `UnifiUser` they already keep its email implicitly only if you pass it; to be safe, add `email=existing.email` to both of those `UnifiUser(...)` reconstructions as well.)

In the `to_update_policy` block add `email=existing.email`, and in the `to_deactivate` block add `email=existing.email`.

- [ ] **Step 6: Run the full reconciler suite**

Run: `uv run pytest tests/test_reconciler.py -v`
Expected: PASS (including `test_idempotency_canary`)

- [ ] **Step 7: Commit**

```bash
git add src/door_sync/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): reconcile email changes via to_update_credential (case-insensitive)"
```

---

## Task 4: Read primary email from CiviCRM

**Files:**
- Modify: `src/door_sync/civicrm/client.py`
- Test: `tests/test_civicrm_client.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_civicrm_client.py`, update the `_contact` helper to include an email key, and update the existing select-assertion test, then add mapping tests.

Update `_contact`:

```python
def _contact(
    contact_id: int,
    display_name: str = "Test Person",
    card_id: int | str = 100,
    email: str | None = "person@example.com",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": contact_id,
        "display_name": display_name,
        "Door_Access.card_id": card_id,
    }
    if email is not None:
        row["email_primary.email"] = email
    return row
```

Update the existing select-assertion test (the one asserting `params["select"]`) to expect the new column:

```python
    assert params["select"] == [
        "id",
        "display_name",
        "Door_Access.card_id",
        "email_primary.email",
    ]
```

Add mapping tests at the end of the file:

```python
def test_fetch_active_maps_primary_email(httpx_mock: HTTPXMock) -> None:
    _register_contacts(httpx_mock, [_contact(42, "Jane Doe", email="jane@example.com")])
    _register_memberships(httpx_mock, [_membership(42, "Gold", "Current")])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result[0].email == "jane@example.com"


def test_fetch_active_missing_email_is_none(httpx_mock: HTTPXMock) -> None:
    _register_contacts(httpx_mock, [_contact(42, "Jane Doe", email=None)])
    _register_memberships(httpx_mock, [_membership(42, "Gold", "Current")])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result[0].email is None


def test_fetch_active_empty_email_string_is_none(httpx_mock: HTTPXMock) -> None:
    contact = _contact(42, "Jane Doe")
    contact["email_primary.email"] = ""
    _register_contacts(httpx_mock, [contact])
    _register_memberships(httpx_mock, [_membership(42, "Gold", "Current")])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result[0].email is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_civicrm_client.py -k "email or select" -v`
Expected: FAIL — select-assertion test fails (missing `email_primary.email`); `test_fetch_active_maps_primary_email` fails (`assert None == 'jane@example.com'`).

- [ ] **Step 3: Add the select column**

In `src/door_sync/civicrm/client.py`, in `_fetch_contacts`, add `"email_primary.email"` to the `select` list:

```python
                {
                    "select": [
                        "id",
                        "display_name",
                        self._config.card_id_field,
                        "email_primary.email",
                    ],
                    "where": [
                        [self._config.card_id_field, "IS NOT EMPTY"],
                        ["is_deleted", "=", False],
                    ],
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                },
```

- [ ] **Step 4: Map the email onto CiviMember**

In `fetch_active`, inside the `for c in contacts:` loop, after computing `card_id` and before constructing the `CiviMember`, add:

```python
            raw_email = c.get("email_primary.email")
            email = raw_email if isinstance(raw_email, str) and raw_email else None
```

Then add `email=email` to the `CiviMember(...)` construction:

```python
            result.append(
                CiviMember(
                    contact_id=cid,
                    display_name=str(c["display_name"]),
                    card_id=card_id,
                    membership_types=tuple(types_by_contact.get(cid, [])),
                    email=email,
                )
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_civicrm_client.py -v`
Expected: PASS (all CiviCRM client tests)

- [ ] **Step 6: Commit**

```bash
git add src/door_sync/civicrm/client.py tests/test_civicrm_client.py
git commit -m "feat(civicrm): select and map contact primary email"
```

---

## Task 5: Read user_email from UniFi

**Files:**
- Modify: `src/door_sync/unifi/client.py`
- Test: `tests/test_unifi_client.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_unifi_client.py`, update the `_user_row` helper to accept an email and include it when set, then add read tests. Update `_user_row` signature and body:

```python
def _user_row(
    contact_id: int = 42,
    user_id: str = "uuid-42",
    first_name: str = "Jane",
    last_name: str = "Doe",
    status: str = "ACTIVE",
    nfc_id: str = "2A04D2",
    policy_id: str = "pol-1",
    nfc_token: str = "tok-42",
    user_email: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": user_id,
        "first_name": first_name,
        "last_name": last_name,
        "employee_number": str(contact_id),
        "status": status,
        "nfc_cards": [{"id": "100001", "nfc_id": nfc_id, "token": nfc_token}],
        "access_policy_ids": [policy_id],
    }
    if user_email is not None:
        row["user_email"] = user_email
    return row
```

Add read tests near the other `fetch_users` tests:

```python
def test_fetch_users_parses_user_email(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_email="jane@example.com")]),
    )
    client = _make_client()
    users = client.fetch_users()
    assert users[0].email == "jane@example.com"
    client.close()


def test_fetch_users_missing_user_email_is_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42)]),  # no user_email key
    )
    client = _make_client()
    users = client.fetch_users()
    assert users[0].email is None
    client.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_unifi_client.py -k "user_email" -v`
Expected: FAIL — `test_fetch_users_parses_user_email` fails (`assert None == 'jane@example.com'`).

- [ ] **Step 3: Parse user_email in _row_to_unifi_user**

In `src/door_sync/unifi/client.py`, in `_row_to_unifi_user`, after computing `active` and before the `return UnifiUser(...)`, add:

```python
        email_raw = row.get("user_email") or ""
        email = str(email_raw) if email_raw else None
```

Then add `email=email` to the `UnifiUser(...)` construction:

```python
        return UnifiUser(
            contact_id=contact_id,
            display_name=display_name,
            card_id=card_id,
            active=active,
            policy=policy,
            email=email,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_unifi_client.py -k "fetch_users or user_email" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "feat(unifi): parse user_email when reading users"
```

---

## Task 6: Write user_email on create, reactivate, and update

**Files:**
- Modify: `src/door_sync/unifi/client.py`
- Test: `tests/test_unifi_client.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_unifi_client.py`. They follow the existing `test_apply_create_new_user_path` / `test_apply_update_credential_name_only` fixture style.

```python
def test_apply_create_includes_user_email_when_set(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new user with an email POSTs user_email in the create body."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={"code": "SUCCESS", "msg": "success", "data": {"id": "uuid-new"}},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email="jane@example.com",
    )
    client.apply(_diff(to_add=(resolved,)))

    post_user = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/developer/users"
    )
    body = _json.loads(post_user.content)
    assert body == {
        "first_name": "Jane",
        "last_name": "Doe",
        "employee_number": "42",
        "user_email": "jane@example.com",
    }
    client.close()


def test_apply_create_omits_user_email_when_none(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new user without an email POSTs no user_email key (unchanged body)."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={"code": "SUCCESS", "msg": "success", "data": {"id": "uuid-new"}},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email=None,
    )
    client.apply(_diff(to_add=(resolved,)))

    post_user = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/developer/users"
    )
    body = _json.loads(post_user.content)
    assert body == {"first_name": "Jane", "last_name": "Doe", "employee_number": "42"}
    client.close()


def test_apply_update_credential_email_only(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """email changes but name and card don't: one PUT carrying only user_email."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [_user_row(contact_id=42, user_id="uuid-42", user_email="old@example.com")]
        ),
    )
    fetched = client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",  # same name as _user_row default
        card_id=1234,  # same card
        target_policy="pol-1",
        resolution="tier",
        email="new@example.com",
    )
    client.apply(_diff(to_update_credential=((resolved, fetched[0]),)))

    put_req = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    )
    body = _json.loads(put_req.content)
    assert body == {"user_email": "new@example.com"}
    # No card-binding calls, since card_id is unchanged.
    user_nfc_calls = [
        r for r in httpx_mock.get_requests() if "/users/uuid-42/nfc_cards" in str(r.url)
    ]
    assert user_nfc_calls == []
    client.close()


def test_apply_update_credential_name_and_email_single_put(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """name AND email change together: a single PUT carries all changed fields."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    first_name="Old",
                    last_name="Name",
                    user_email="old@example.com",
                )
            ]
        ),
    )
    fetched = client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="New Name",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email="new@example.com",
    )
    client.apply(_diff(to_update_credential=((resolved, fetched[0]),)))

    put_reqs = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    ]
    assert len(put_reqs) == 1  # one combined PUT, not two
    body = _json.loads(put_reqs[0].content)
    assert body == {"first_name": "New", "last_name": "Name", "user_email": "new@example.com"}
    client.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_unifi_client.py -k "user_email or name_and_email" -v`
Expected: FAIL — create test expects `user_email` not yet sent; `email_only` test fails because the name-PUT currently fires only on name change (no PUT made when name unchanged).

- [ ] **Step 3: Add a local case-insensitive email helper**

`unifi/client.py` is *higher* than `reconciler.py` in the dependency table (architecture §4) and must not import it. Add a module-private helper near the other module-level helpers (e.g., next to `_split_name`):

```python
def _email_differs_ci(a: str | None, b: str | None) -> bool:
    """Case-insensitive email comparison; empty string and None are equal.

    Duplicated from reconciler by design — importing reconciler here would
    violate the strict layering in architecture §4. Both copies must agree.
    """
    na = a.lower() if a else None
    nb = b.lower() if b else None
    return na != nb
```

- [ ] **Step 4: Write user_email on create**

In `_create_user`, build the body conditionally:

```python
    def _create_user(self, resolved: ResolvedMember, first: str, last: str) -> str:
        body: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
            "employee_number": str(resolved.contact_id),
        }
        if resolved.email is not None:
            body["user_email"] = resolved.email
        data = self._request("POST", "/api/v1/developer/users", json=body)
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
        if not isinstance(data, dict) or "id" not in data:
            raise UnifiClientError(f"POST /users returned no id for contact={resolved.contact_id}")
        return str(data["id"])
```

- [ ] **Step 5: Write user_email on reactivation**

In `_prepare_reactivation`, the first `PUT` sets name + employee_number. Send `user_email` **unconditionally** as `resolved.email or ""`. Unlike create, reactivation targets an existing record that may carry a stale email, so an absent email is sent as `""` to clear it in the same cycle (matching the update path). Change the json body construction:

```python
        body: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
            "employee_number": str(resolved.contact_id),
            "user_email": resolved.email or "",
        }
        self._request(
            "PUT",
            f"/api/v1/developer/users/{user_id}",
            json=body,
        )
        time.sleep(self._INTER_CALL_DELAY_SECONDS)
```

(Leave the stale-card deletion logic below it unchanged.)

- [ ] **Step 6: Restructure _apply_update_credential to combine name + email in one PUT**

Replace the name-update block at the top of the `for resolved, unifi_user in diff.to_update_credential:` loop body (currently a standalone `if resolved.display_name != unifi_user.display_name:` PUT) with a combined field-collection PUT. The card-handling block below it stays exactly as-is:

```python
            user_fields: dict[str, Any] = {}
            if resolved.display_name != unifi_user.display_name:
                first, last = _split_name(resolved.display_name)
                user_fields["first_name"] = first
                user_fields["last_name"] = last
            if _email_differs_ci(resolved.email, unifi_user.email):
                # Empty string clears the email in UniFi; a value sets it.
                user_fields["user_email"] = resolved.email or ""
            if user_fields:
                self._request(
                    "PUT",
                    f"/api/v1/developer/users/{user_id}",
                    json=user_fields,
                )
                time.sleep(self._INTER_CALL_DELAY_SECONDS)
```

- [ ] **Step 7: Add an email note to the dry-run credential-update log line**

In `_log_dry_run_actions`, the `would-update-credential` loop currently logs old/new card. Append an email indicator so a dry-run report shows email changes. Replace that loop body:

```python
        for resolved, unifi_user in diff.to_update_credential:
            email_change = (
                " email-change" if _email_differs_ci(resolved.email, unifi_user.email) else ""
            )
            logger.info(
                "would-update-credential contact=%d old_card=%s new_card=%s%s",
                resolved.contact_id,
                _redact(unifi_user.card_id),
                _redact(resolved.card_id),
                email_change,
            )
```

(Email addresses are not credential material and are not redacted; the dry-run line deliberately states only that an email change *exists*, not the address, to keep journal lines short.)

- [ ] **Step 8: Run the UniFi client suite**

Run: `uv run pytest tests/test_unifi_client.py -v`
Expected: PASS — new tests pass; existing `test_apply_create_new_user_path` and `test_apply_update_credential_name_only` still pass (their resolved members have `email=None`, so bodies are unchanged).

- [ ] **Step 9: Commit**

```bash
git add src/door_sync/unifi/client.py tests/test_unifi_client.py
git commit -m "feat(unifi): write user_email on create, reactivate, and credential update"
```

---

## Task 7: Warn on missing email for door-tier members (orchestrator)

**Files:**
- Modify: `src/door_sync/orchestrator.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
def test_warns_when_tier_member_has_no_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A member resolving to a door tier with no CiviCRM email logs one WARN
    but is still provisioned (warn-but-provision)."""
    cfg = _config(tmp_path)
    members = [
        CiviMember(
            contact_id=i,
            display_name=f"User {i}",
            card_id=0x1000 + i,
            membership_types=("Gold",),
            email="present@example.com",
        )
        for i in range(1, 12)
    ]
    members.append(
        CiviMember(
            contact_id=99,
            display_name="No Email",
            card_id=0x9999,
            membership_types=("Gold",),
            email=None,
        )
    )
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 12)
    ]
    holder = _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    with caplog.at_level(logging.WARNING, logger="door_sync.orchestrator"):
        result = orchestrator.reconcile(cfg, dry_run=False)

    assert result.halted is False
    warnings = [r for r in caplog.records if "no email" in r.message and "99" in r.message]
    assert len(warnings) == 1
    # Still provisioned: contact 99 is in to_add.
    unifi: FakeUnifiClient = holder["unifi"]
    assert any(m.contact_id == 99 for m in unifi.apply_calls[0].to_add)


def test_no_warn_when_non_tier_member_has_no_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A member with no membership (resolves to 'none') and no email does NOT warn."""
    cfg = _config(tmp_path)
    members = [
        CiviMember(
            contact_id=i,
            display_name=f"User {i}",
            card_id=0x1000 + i,
            membership_types=("Gold",),
            email="present@example.com",
        )
        for i in range(1, 13)
    ]
    members.append(
        CiviMember(
            contact_id=99, display_name="No Tier", card_id=0x9999, membership_types=(), email=None
        )
    )
    users = [
        UnifiUser(
            contact_id=i, display_name=f"User {i}", card_id=0x1000 + i, active=True, policy="p1"
        )
        for i in range(1, 13)
    ]
    _patch_clients(monkeypatch, civi_members=members, unifi_users=users)

    with caplog.at_level(logging.WARNING, logger="door_sync.orchestrator"):
        orchestrator.reconcile(cfg, dry_run=False)

    warnings = [r for r in caplog.records if "no email" in r.message]
    assert warnings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator.py -k "no_email or non_tier" -v`
Expected: FAIL — `test_warns_when_tier_member_has_no_email` fails (no WARN emitted yet).

- [ ] **Step 3: Add the warning loop in reconcile**

In `src/door_sync/orchestrator.py`, inside `reconcile`, immediately after the `resolved = [...]` list comprehension and before `unifi_users = unifi.fetch_users()`:

```python
        resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
        for r in resolved:
            if r.resolution == "tier" and r.email is None:
                _logger.warning(
                    "contact %d resolves to a door tier but has no email in CiviCRM; "
                    "provisioning without invite delivery",
                    r.contact_id,
                )
        unifi_users = unifi.fetch_users()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS (all orchestrator tests; existing ones unaffected because their members default `email=None` but resolve identically, and pre-existing tests don't assert on WARN records)

- [ ] **Step 5: Commit**

```bash
git add src/door_sync/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): warn when a door-tier member has no email"
```

---

## Task 8: Update architecture doc and run full verification

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: Update the §6 data contracts**

In `docs/architecture.md` §6, update the three dataclass definitions in the code block to include the new field, keeping the reference authoritative:

- `CiviMember`: add `email: str | None = None` after `membership_types`.
- `ResolvedMember`: add `email: str | None = None` after `resolution`.
- `UnifiUser`: add `email: str | None = None` after `policy`.

Add a sentence to the "Naming note" paragraph after the code block:

```text
The `email` field rides `to_update_credential` for the same reason as
`display_name`: it is written via the same `PUT /users/{id}` endpoint. It is
compared case-insensitively so harmless case differences don't churn.
```

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest`
Expected: PASS — all tests green.

- [ ] **Step 3: Type check**

Run: `uv run pyrefly check`
Expected: no errors.

- [ ] **Step 4: Lint and format check**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: no issues. (If format flags files, run `uv run ruff format .` and re-stage.)

- [ ] **Step 5: Dry-run smoke (optional, if a config is available)**

Run: `uv run door-sync show-diff` or `uv run door-sync run --once --dry-run`
Expected: runs without error; any email changes appear as `would-update-credential ... email-change` lines. Skip if no local config/credentials are configured.

- [ ] **Step 6: Commit**

```bash
git add docs/architecture.md
git commit -m "docs(architecture): document email field on data contracts"
```

---

## Self-Review Notes

- **Spec coverage:** models (T1), CiviCRM read (T4), tier passthrough (T2), reconciler compare (T3), UniFi read (T5) + write (T6), orchestrator warn (T7), idempotency (T3 canary), tests across all, docs (T8). All spec §3 subsections map to a task.
- **Default-None rationale:** chosen so existing strict-equality body assertions stay green; new behavior is purely additive and gated on a present email.
- **Layering:** `_email_differs_ci` is duplicated into `unifi/client.py` rather than imported from `reconciler.py` (architecture §4 forbids the import edge). Both copies use the same lowercase-or-None rule.
- **Card-ID redaction unaffected:** email is not redacted; dry-run line states only that an email change exists, not the address.
- **Risk carried from spec:** the `GET /users` read field is assumed to be `user_email` (same as write). If the read field differs, only Step 3 of Task 5 changes. The idempotency canary (T3) and a real-controller dry-run (T8 Step 5) would surface any read/write field-name mismatch as a persistent `to_update_credential`.
