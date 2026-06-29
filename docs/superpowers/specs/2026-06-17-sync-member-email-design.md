# Design: Sync member email from CiviCRM to UniFi Access

**Date:** 2026-06-17
**Status:** Approved (brainstorming) — ready for implementation plan
**Scope:** Add a member's primary email to the reconciliation flow so it is
provisioned and kept in sync on the UniFi Access user record.

---

## 1. Motivation

UniFi Access uses a user's email **functionally**: it delivers mobile
credential invites and PIN codes to that address. Today the reconciler syncs
`display_name` and `card_id` but not email, so UniFi users are created without
an email and the field drifts from CiviCRM. This change makes CiviCRM's primary
email the source of truth for the UniFi `user_email` field.

Decision context (from brainstorming):

- **Functional, not cosmetic** — email correctness matters because credential
  delivery depends on it. So email differences must trigger reconciliation, not
  just be set once at create time.
- **Missing email → warn but provision** — a member with a card but no primary
  email is still provisioned normally (door access does not depend on email);
  the cycle emits one WARN per such member so the gap is visible in the journal
  without halting.
- **UniFi field name is `user_email`** (confirmed against the controller), set
  on `POST/PUT /api/v1/developer/users`.

---

## 2. Architectural fit

This change adds **one optional field** that rides the existing data flow
`CiviMember → ResolvedMember → Diff → UnifiUser`. It introduces no new module,
no new diff set, and no change to the pure/impure boundary or the safety guards.

The architecture doc (§6, naming note) already establishes the precedent:
`display_name` lives in `to_update_credential` — not because it is a credential,
but because it shares the `PUT /users/{id}` endpoint with the card write.
**Email uses that same endpoint, so it belongs in `to_update_credential`
alongside `display_name`.** Reusing this set means `reconciler.compute_diff`'s
shape, `safety.check`, and all five `Diff` sets stay exactly as they are.

Hard rules respected:

- Pure modules (`reconciler`, `tier_mapping`, `safety`) stay pure — email is a
  plain passthrough field; no logging, no config lookups, no new exceptions.
- Frozen dataclasses — email is a new field on existing frozen models; nothing
  is mutated.
- Card-ID redaction is unaffected (email is not credential material and is not
  redacted).
- Dry-run stays sacred — email writes go through the same `UnifiClient` write
  path that the dry-run flag already neutralizes.

---

## 3. Changes by module

### 3.1 `models.py` — add one field to three dataclasses

```python
@dataclass(frozen=True)
class CiviMember:
    ...
    email: str | None      # primary email from CiviCRM, or None

@dataclass(frozen=True)
class ResolvedMember:
    ...
    email: str | None      # carried through from CiviMember

@dataclass(frozen=True)
class UnifiUser:
    ...
    email: str | None      # user_email as read from UniFi, or None
```

Update each class docstring's `Parameters:` block. Field ordering: append after
the existing fields to minimize churn in positional constructions (tests use
keyword args, but keep it tidy).

### 3.2 `civicrm/client.py` — select and map the primary email

- Add `"email_primary.email"` to the `Contact.get` `select` list in
  `_fetch_contacts`. No new query or extra round-trip — one more column on the
  existing paginated call.
- In `fetch_active`, read the value off each contact row and normalize empty /
  missing to `None`:

  ```python
  raw_email = c.get("email_primary.email")
  email = raw_email if isinstance(raw_email, str) and raw_email else None
  ```

- Pass `email=email` into the `CiviMember(...)` construction.

No change to the membership query or retry logic. No exception on a missing
email — `None` is a valid value that flows downstream.

### 3.3 `tier_mapping.py` — passthrough (pure)

Every `ResolvedMember(...)` construction in `resolve` (all four branches: none,
unmapped, and the matched tier/none/day-pass path) gains `email=member.email`.
No logic, no branching on email. `resolve_all` is unchanged.

### 3.4 `reconciler.py` — extend the credential-changed check (pure)

The only behavioral line is the `cred_changed` computation. Email is compared
**case-insensitively** to avoid update churn from harmless case differences:

```python
def _email_differs(resolved_email: str | None, unifi_email: str | None) -> bool:
    """Case-insensitive email comparison; None != "" handled by normalize."""
    return _norm(resolved_email) != _norm(unifi_email)

def _norm(email: str | None) -> str | None:
    return email.lower() if email else None
```

```python
cred_changed = (
    u.card_id != r.card_id
    or u.display_name != r.display_name
    or _email_differs(r.email, u.email)
)
```

`_norm` collapses both `None` and `""` to `None`, so a CiviCRM-empty email and
a UniFi-absent email do not register as a difference. The helper functions are
module-private and pure (no I/O). `to_update_policy`, `to_deactivate`,
`to_add`, and `unmapped` are untouched.

### 3.5 `unifi/client.py` — read and write `user_email`

- **Read** (`_row_to_unifi_user`): parse the email into `UnifiUser.email`:

  ```python
  email_raw = row.get("user_email") or ""
  email = str(email_raw) if email_raw else None
  ```

  Pass `email=email` into the `UnifiUser(...)` construction.

  > Note: the API field name returned by `GET /users` is assumed to be
  > `user_email` (same as the write field). Confirm against a live response
  > during implementation; if the read field differs, only this line changes.

- **Create** (`_create_user`): include `user_email` in the POST body when set:

  ```python
  body = {"first_name": first, "last_name": last,
          "employee_number": str(resolved.contact_id)}
  if resolved.email is not None:
      body["user_email"] = resolved.email
  ```

- **Reactivate** (`_prepare_reactivation`): send `user_email` **unconditionally**
  as `resolved.email or ""`. Unlike create, reactivation targets an existing
  record that may already carry a stale email, so an absent email must be sent
  as `""` to clear it in the same cycle — matching the update path. (Create
  omits the field when absent because a brand-new record has nothing to clear.)

  ```python
  body["user_email"] = resolved.email or ""
  ```

- **Update** (`_apply_update_credential`): email travels with the name update so
  a combined name+email change is a single `PUT`, not two. Today the name PUT
  fires only when `display_name` differs; restructure so the PUT fires when
  **name or email** differs, sending whichever fields changed:

  ```python
  user_fields: dict[str, Any] = {}
  if resolved.display_name != unifi_user.display_name:
      first, last = _split_name(resolved.display_name)
      user_fields["first_name"] = first
      user_fields["last_name"] = last
  if _email_differs_ci(resolved.email, unifi_user.email):
      # send empty string to clear, value to set
      user_fields["user_email"] = resolved.email or ""
  if user_fields:
      self._request("PUT", f"/api/v1/developer/users/{user_id}", json=user_fields)
      time.sleep(self._INTER_CALL_DELAY_SECONDS)
  ```

  The card-update block below it is unchanged.

  **Layering note:** `unifi/client.py` is *higher* than `reconciler.py` in the
  dependency table (architecture §4), so it must **not** import the reconciler's
  helper. The client gets its own module-private `_email_differs_ci` /
  case-insensitive normalize (a few trivial lines, duplicated by design rather
  than creating an illegal import edge). Both copies must agree on the
  case-insensitive rule; a test asserts the no-op behavior on case-only
  differences on both sides. This guard exists so that when only the card
  changed, the name/email PUT is skipped (no empty write).

- **Dry-run logging** (`_log_dry_run_actions`): extend the
  `would-update-credential` line to mention an email change when present
  (email is not redacted). Keep it informative but low-noise.

### 3.6 `orchestrator.py` — warn on missing email for provisioned members

After resolution and before (or alongside) the diff, the orchestrator inspects
the resolved set and logs one WARN per member who will be provisioned but has no
email. This lives here — not in a pure module — because the warning is a logging
side effect and is scoped to members who actually need email (`resolution ==
"tier"`):

```python
resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in civi_members]
for r in resolved:
    if r.resolution == "tier" and r.email is None:
        _logger.warning("contact %d resolves to a door tier but has no email "
                        "in CiviCRM; provisioning without invite delivery",
                        r.contact_id)
```

Members resolving to `none`, `day-pass`, or `unmapped` are not warned about —
they will not receive credential invites regardless. No change to the halt /
apply / state-write flow.

---

## 4. Idempotency

The canary test (`compute_diff` immediately after `apply()` yields all-empty
sets) still holds: email now round-trips — written via `user_email` on
create/update, read back via `_row_to_unifi_user`, and compared
case-insensitively. A member created with email `Foo@Bar.com` reads back as
`Foo@Bar.com` (or as the controller normalizes it); the case-insensitive
compare prevents a normalization round-trip from registering as a perpetual
diff.

> Risk to verify in implementation: if UniFi lowercases stored emails, the
> read-back differs in case from CiviCRM — the case-insensitive compare already
> absorbs this. If UniFi rewrites the address in some other way (trimming,
> plus-addressing), that surfaces as a persistent `to_update_credential` and
> the idempotency test would catch it against a real controller fixture.

---

## 5. Testing

All pure-module tests stay mock-free (plain dataclass construction).

- **`test_reconciler.py`**
  - email-only change (same card, same name, different email) → member in
    `to_update_credential`.
  - case-only email difference (`a@x.com` vs `A@X.com`) → **no-op**.
  - CiviCRM-empty (`None`) vs UniFi-absent (`None` or `""`) → **no-op**.
  - idempotency canary updated to carry email through.
- **`test_tier_mapping.py`** — email passthrough preserved across all
  resolution branches (tier, none, unmapped, day-pass).
- **`test_orchestrator.py`** — a `tier`-resolved member with `email is None`
  produces the WARN log (assert via `caplog`); provisioning still proceeds.
- **UniFi client tests** (fakes, matching the existing fixture style) — create
  and update bodies include `user_email` when set and omit it when `None`;
  `_row_to_unifi_user` parses `user_email`.

---

## 6. Out of scope (YAGNI)

- Making the CiviCRM email field configurable (like `card_id_field`). Primary
  email via `email_primary.email` is the standard API4 path; add config only if
  a deployment needs a non-primary email location.
- Email as a safety-guard input. Email changes ride the existing
  `mass_*` thresholds via `to_update_credential`; no new guard.
- Phone or any other contact field. This change is email only.
- Validating email format. CiviCRM is trusted as the source of truth; UniFi
  rejects malformed addresses at its own API boundary, surfacing as a
  `UnifiClientError` the scheduler already handles.
