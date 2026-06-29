# UniFi Access client — design

**Date:** 2026-05-22
**Status:** Approved for planning
**Companion:** [`docs/architecture.md`](../../architecture.md) §4 (module table — `unifi.client`), §5 (dry-run mechanism), §7 step 3 + step 6 (UniFi data we need, apply ordering), §10 (orchestrator integration), §11 (HTTP/error/redaction conventions). This spec also closes architecture §12's "UniFi client API surface" deferral.

---

> **Correction (2026-06-29) — read path no longer uses `nfc_id`; supersedes §8, §9, §10's read claims, and §11.**
>
> §10 below states "UniFi exposes `nfc_id` on each card record." That is **only true of the `POST /credentials/nfc_cards/import` response.** The read endpoints do **not** return `nfc_id`:
> - `GET /users` → each `nfc_cards[]` entry is `{id (display), token, type}`.
> - `GET /credentials/nfc_cards/tokens` → each card is `{display_id, alias, token, ...}`.
>
> Because the original code read `nfc_cards[0]["nfc_id"]` from `/users` (and `nfc_id` from the card list), every read produced `card_id=None` and the token map was always empty. Current behavior:
> - A card's number is recovered from the door-sync import **alias** `sync-<card_id>` (`_parse_sync_alias`), joined to users by **`token`**. `_ensure_nfc_token_map()` builds both `{card_id → token}` and the reverse `{token → card_id}`, keyed off `alias`; `fetch_users()` loads it to resolve each user's `card_id`. Only door-sync-imported cards (which carry the alias) are recognized; everything else resolves to `card_id=None`. The §10 `_compute_nfc_id`/`_parse_nfc_id` helpers and the FC-mismatch behavior in §10 now apply **only to the import path** (CSV upload + import-response parsing), not to user/card reads.
>
> §11's access-policy handling ("take the first of `access_policy_ids`") was also revised: `UnifiClient` now takes `managed_policy_ids` (the `tier_mapping` target policies). It ignores any policy not in that set on read (so a policy UniFi auto-applies to all users is not mistaken for tier drift) and sends only the tier policy on write. See `docs/architecture.md` §7 and §8 for the current contracts.

---

## 1. Goal

Implement the read+write UniFi Access client that the orchestrator calls once per reconcile cycle:

- `UnifiClient(config.unifi, dry_run=...).fetch_users() -> list[UnifiUser]`
- `UnifiClient(...).apply(diff) -> None`

When this slice ships:

- The client can list users, create users, update users (name + status), assign access policies, register third-party NFC cards (Wiegand 26-bit H10301) via CSV import, and bind cards to users — end-to-end against a real UniFi Access controller (≥ v3.3.10) over its self-signed-cert local API.
- Dry-run is honored: writes become redacted log lines, reads still execute.
- TLS connection is fingerprint-pinned per [architecture §11](../../architecture.md).
- Tests cover happy paths, dry-run, retries, error envelopes, fingerprint mismatch, and the import-then-bind ordering via `pytest-httpx`.

## 2. Definition of done

All three commands green:

```bash
uv run pytest
uv run mypy --strict src tests
uv run ruff check .
```

Plus:

- `src/door_sync/unifi/__init__.py` and `src/door_sync/unifi/client.py` exist
- `tests/test_unifi_client.py` exists with at least the tests listed in §13
- `UnifiConfig` gains a `facility_code: int` attribute (validated 0–255), and the existing `host`/`api_key`/`tls_fingerprint` fields are unchanged
- `config.example.toml` documents the new `facility_code` field
- `UnifiClient` exposes one read method (`fetch_users`), one write method (`apply`), one public exception (`UnifiClientError`), and a context-manager protocol
- Sync `httpx` only — no asyncio, no streaming
- Card-ID redaction applied to every log statement that mentions a card

## 3. Non-goals (deferred)

- **Touch Pass (mobile credential) management.** The reconciler doesn't issue mobile passes; out of scope. (UniFi sections 3.25–3.27, 6.11–6.17.)
- **PIN code management.** Not issued by the reconciler.
- **Door / device / schedule / holiday administration.** UniFi sections 5, 7, 8 — not consumed by the reconciler.
- **Visitor / day-pass flow.** Architecture Appendix C; lands later in `webhook.py` with a separate Visitor-scope API key.
- **License plate credentials.** Not in scope.
- **Async / streaming.** Sync `httpx` per architecture §3.
- **Hard delete of users.** The reconciler deactivates (sets `status: "DEACTIVATED"`); it never calls `DELETE /users/:id`. Hard delete is reserved for human admin action.
- **Connection re-pinning mid-session.** TLS fingerprint is verified once at construction time. Subsequent calls in the same cycle use `verify=False`. See §6 risk.
- **Per-card Wiegand format selection.** v1 assumes all CiviCRM card_ids are Wiegand 26-bit (H10301). Mixing formats (34-bit, raw NFC UID) would require extending `CiviMember` with a per-card format hint; deferred.
- **`Retry-After` HTTP-date format.** Only the seconds form is honored.

## 4. The class

```python
class UnifiClient:
    def __init__(self, config: UnifiConfig, *, dry_run: bool = False) -> None: ...
    def fetch_users(self) -> list[UnifiUser]: ...
    def apply(self, diff: Diff) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> "UnifiClient": ...
    def __exit__(self, *args: object) -> None: ...


class UnifiClientError(Exception):
    """Raised on any non-recoverable UniFi Access API failure."""
```

One `httpx.Client` per instance, constructed per cycle (mirrors `CivicrmClient`). The class is constructed per reconcile cycle — no module-level singletons.

The constructor takes the whole `UnifiConfig` slice. The `dry_run` kwarg is the orchestrator's seam: same flag passed through from `reconcile(..., dry_run=...)`.

## 5. HTTP contract

- **Base URL:** `https://<config.host>:12445/api/v1/developer/`. Port `12445` is fixed by UniFi Access; we hardcode it.
- **Auth:** `Authorization: Bearer <config.api_key>` header on every request.
- **Content type:** `application/json` for JSON bodies; `multipart/form-data` for the one CSV upload (section 6.19).
- **Response envelope (universal):**
  ```json
  {"code": "SUCCESS" | "CODE_*", "msg": "...", "data": <payload or null>}
  ```
  A helper `_unwrap(response) -> Any` extracts `data` on `code == "SUCCESS"`, raises `UnifiClientError(f"{code}: {msg}")` otherwise. This wraps every API call.
- **httpx config:** `base_url=f"https://{config.host}:12445"`, `timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)`, `verify=False` (we pin via fingerprint — see §6), no following redirects.

## 6. TLS fingerprint pinning

**Approach: verify-once-at-construction, then `verify=False` for the session.**

In `__init__`, before constructing the `httpx.Client`:

1. Parse `config.host` to hostname + (default) port `12445`.
2. Open a raw `socket.create_connection((host, 12445), timeout=10)`.
3. Wrap with an `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` that has `check_hostname=False` and `verify_mode=ssl.CERT_NONE`.
4. `getpeercert(binary_form=True)` → SHA-256 hash → compare (case-insensitive, strip colons) against `config.tls_fingerprint`.
5. Mismatch → raise `UnifiClientError("TLS fingerprint mismatch: expected <expected>, got <actual>")`. The `httpx.Client` is **not** constructed in this case.
6. Match → construct `httpx.Client(verify=False, …)` for the session.

**Risk and mitigation.** Connections within the cycle aren't individually re-pinned. The threat model: between init-time verification and a later API call, a MITM could substitute a different cert. Mitigations: the cycle is short, the controller is on a trusted LAN at a fixed IP, and a new client is built every cycle (architecture §10). Documented as an acknowledged risk in §15; can be hardened to a custom `httpx.HTTPTransport` later if the threat model tightens.

## 7. `fetch_users()`

`GET /api/v1/developer/users?page_num=<n>&page_size=100&expand[]=access_policy`, paginated. Response envelope's `data` is a list of user objects; `pagination.total` indicates the full count.

For each row:

- Parse `employee_number` as `int`. If non-int or empty: **skip** (admin-created user, not sync-managed; architecture §7 step 3).
- Cache `unifi_user_id` (the UUID `id` field) in an internal `dict[contact_id → str]` for use during `apply()`.
- Cache `nfc_cards` list (each entry has `id` (display string), `token`, `type`) in an internal `dict[contact_id → list[NfcCardRef]]` for the same reason.
- Build a `UnifiUser`:
  - `contact_id = int(employee_number)`
  - `display_name = " ".join([first_name, last_name]).strip()`
  - `card_id = _parse_nfc_id(nfc_cards[0]["nfc_id"], config.facility_code)` — see §10; `None` if no cards, the nfc_id is unparseable, or its encoded FC differs from `config.facility_code` — **[SUPERSEDED — see Correction (2026-06-29)]** `/users` cards have no `nfc_id`; `card_id` is recovered from the card `token` via the `sync-<card_id>` alias.
  - `active = status == "ACTIVE"`
  - `policy = access_policy_ids[0]` — see §11; `None` if no policies — **[SUPERSEDED — see Correction (2026-06-29)]** `policy` is now the single *managed* policy (filtered by `managed_policy_ids`), not the raw first.

If a user has multiple cards or multiple policies, we use the first by `id`-ordered iteration and emit a `logger.warning("contact %d has %d cards/policies; using the first", …)` with redacted card-ID. This is operational drift the reconciler reports but does not auto-correct — the safety guards and human review handle it.

Pagination follows the same shape as `CivicrmClient`: a `_MAX_PAGES = 1_000` ceiling raises `UnifiClientError` if exceeded.

## 8. `apply()`

**Precondition.** `fetch_users()` must have been called on the same instance first. If `apply()` is called before, raise `UnifiClientError("apply() requires a prior fetch_users() call")`. The orchestrator's `reconcile()` already enforces this ordering.

**Dry-run.** Each would-be HTTP call is replaced by `logger.info("would-<verb> contact=%d card=%s policy=%s", …)` with redacted card-ID. No HTTP calls. No inter-call delay. The internal caches and `_nfc_token_map` (see §9) still get populated by the **read** side, since reads execute normally in dry-run.

**Live order of operations** (per cycle):

1. **Build the NFC-token map** (lazy, first time it's needed):
   - `GET /api/v1/developer/credentials/nfc_cards/tokens?page_size=100`, paginated
   - Builds `dict[card_id_int → token_str]` keyed by `_parse_nfc_id(row["nfc_id"], config.facility_code)`. Rows whose nfc_id is un-parseable or whose encoded FC differs from `config.facility_code` are skipped with a debug-level log (admin-managed or foreign-FC cards we don't reconcile).
2. **Import unknown cards** (one batched CSV upload if any are missing):
   - Compute the set of `card_id`s appearing in `diff.to_add` and `diff.to_update_credential` that are *not* in the token map.
   - If empty: skip.
   - Else: assemble a CSV in memory (one row per missing card_id, no header row — see §9 for the exact format), `POST /api/v1/developer/credentials/nfc_cards/import` (multipart, field name `file`).
   - On success, parse the response's `data` array of `{nfc_id, token}` entries and merge into the token map.
3. **Deactivate** — for each entry in `diff.to_deactivate`: `PUT /users/:id` with body `{"status": "DEACTIVATED"}`, then DELETE the user's cached NFC card(s) (`/users/:id/nfc_cards/delete`) so the card number is freed for reuse by a new member. Status first, card removal second: cutting access must not depend on the cleanup succeeding.
4. **Update credential** — for each `(resolved, unifi_user)` in `diff.to_update_credential`:
   - If `display_name` changed: `PUT /users/:id` body `{first_name, last_name}` (split via `_split_name`).
   - If `card_id` changed:
     - If the user has any existing card: `DELETE /users/:id/nfc_cards/delete` with body `{"token": <old_token>}` (resolves old token from the cached `nfc_cards` map populated by `fetch_users()`).
     - `PUT /users/:id/nfc_cards` with body `{"token": <new_token>, "force_add": false}`. Token is looked up in the NFC-token map (post-import).
5. **Update policy** — for each `(resolved, unifi_user)` in `diff.to_update_policy`:
   - `PUT /users/:id/access_policies` with body `{"access_policy_ids": [<resolved.target_policy>]}`.
6. **Add** — for each `resolved` in `diff.to_add`:
   - If `contact_id` is in the cached `unifi_user_id` map: the user exists but is inactive (reactivate path). `PUT /users/:id` with `{first_name, last_name, employee_number, status: "ACTIVE"}`. Then, if the cached user has any pre-existing cards whose tokens differ from the new `resolved.card_id`'s token, DELETE each via `/users/:id/nfc_cards/delete` (same as the credential-update path in step 4) before binding the new card. This handles the case where a contact was deactivated, kept their old card record, and is being re-provisioned with a new card.
   - Else (true create): `POST /users` with `{first_name, last_name, employee_number}` — note `employee_number` is sent as the string form of `contact_id`. Response's `data.id` is the new UniFi user ID; cache it.
   - If `resolved.card_id` is set: bind card via `PUT /users/:id/nfc_cards` with the token from the map.
   - Assign policy: `PUT /users/:id/access_policies` with `{"access_policy_ids": [<resolved.target_policy>]}`.

**Inter-call delay.** `time.sleep(0.075)` between each individual write call (75 ms). The card-import CSV is a single call regardless of how many cards it covers. Dry-run has no delay.

**Error model.** Each individual HTTP call retries per §11. If a call's retry budget is exhausted, `apply()` raises `UnifiClientError` immediately; remaining writes are abandoned. Next cycle re-computes the diff and resumes (architecture §7 step 6).

## 9. The NFC card flow

The reconciler bridges CiviCRM (decimal int card numbers) and UniFi Access (token-keyed cards) via UniFi section 6.19 ("Import Third-Party NFC Cards"). The API endpoint accepts a CSV that is **different from the web UI's 9-column template** — the API form is the simpler one documented in the PDF.

**Verified against the real controller (2026-05-22):** the 2-column CSV `<nfc_id>,<alias>` (no header row) works against `POST /api/v1/developer/credentials/nfc_cards/import` as a multipart upload. The imported card was then bindable to a user and unlocked a door — confirming that API-imported cards become real, reader-recognized credentials.

**CSV format per imported card:**

```csv
{nfc_id},{alias}
```

- No header row.
- `nfc_id` = `_compute_nfc_id(config.facility_code, card_id)` — uppercase hex of `(FC << 16) | CN`, see §10.
- `alias` = `f"sync-{card_id:05d}"` — deterministic, includes the `sync-` prefix so an operator viewing the UniFi UI can tell at a glance which cards are reconciler-managed vs. manually enrolled. Aliases must be unique per UniFi (PDF §6.19); the card_id-derived form is unique as long as we never re-import the same card. If the alias is already in use (e.g. previous manual edit) the import will fail and we raise — operator resolves manually.

**Multipart request shape:**

```
POST /api/v1/developer/credentials/nfc_cards/import
Authorization: Bearer <api_key>
Content-Type: multipart/form-data; boundary=...

(field name: "file"; filename: "cards.csv"; type: "text/csv")
2A04D3,sync-01235
2A04D4,sync-01236
```

In Python: `httpx.post(url, files={"file": ("cards.csv", csv_bytes, "text/csv")}, headers={"Authorization": …})`.

**Import response:**

```json
{
  "code": "SUCCESS",
  "data": [
    {"alias": "sync-01235", "nfc_id": "2A04D3", "token": "<64-hex>"},
    ...
  ],
  "msg": "success"
}
```

We merge each `{nfc_id, token}` into the in-memory token map. Key conversion: `_parse_nfc_id(row["nfc_id"], config.facility_code)` — see §10. A row whose nfc_id doesn't decode under the configured facility code is treated as a failed import and raises `UnifiClientError`. If a row's `token` is empty, the import failed for that record; we raise `UnifiClientError("card import failed for card_id=%d", card_id)` (with redacted card_id in the message).

## 10. Card-ID format conversion

**Verified against the real controller (2026-05-22).** UniFi exposes `nfc_id` on each card record as **uppercase hex of `(facility_code << 16) | card_number`** — the Wiegand-26 (H10301) "useful data" portion with parity bits stripped. The encoding is identical whether the card was enrolled via session-at-reader (`card_type: "id_card"`) or imported via the web UI / CSV path. Verification used two real cards on the production controller; the deployment's actual facility code and card numbers are deliberately omitted from this spec — the illustrative values below demonstrate the same encoding with synthetic numbers.

Encoding examples (synthetic):

| Printed CN | FC | UniFi `nfc_id` | Computed `(FC << 16) \| CN` |
|---|---|---|---|
| 01234 | 42 | `2A04D2` | `0x2A << 16 \| 0x04D2` = `0x2A04D2` ✓ |
| 01235 | 42 | `2A04D3` | `0x2A << 16 \| 0x04D3` = `0x2A04D3` ✓ |

**Note:** the `display_id` field (e.g. `"100003"`, `"100004"`) is a UniFi-internal sequence counter that auto-increments with each enrolled card and is **not** derived from card data. Do not use it for correlation.

**Helpers:**

```python
def _compute_nfc_id(facility_code: int, card_id: int) -> str:
    """Encode a Wiegand-26 (FC, CN) pair the way UniFi does in nfc_id:
    uppercase hex of (FC << 16) | CN, no zero-padding.

    Example: (42, 1234) -> "2A04D2", (42, 1235) -> "2A04D3".
    """
    return f"{(facility_code << 16) | card_id:X}"


def _parse_nfc_id(nfc_id: str, expected_facility_code: int) -> int | None:
    """Decode UniFi's nfc_id back to a Wiegand card_number (CN).

    Returns the CN if the encoded facility code matches
    `expected_facility_code`. Returns None on parse failure or FC
    mismatch — the latter is treated as "card not in our namespace"
    and surfaces as an operational warning at the call site.
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

For matching, the reconciler compares CN integers, not hex strings — that sidesteps any future inconsistency in UniFi's padding/case conventions.

**Behavior on FC mismatch.** If a user has a card bound whose `nfc_id` decodes to a different facility code than `config.facility_code`, the reconciler treats that user as having no managed card (`UnifiUser.card_id = None`) and emits one `logger.warning("contact %d has foreign-FC card nfc_id=%s; skipping", contact_id, _redact(nfc_id))`. The next diff cycle will then try to add a card with the configured FC, which will either succeed (creating a second binding) or fail (UniFi rejects duplicates) — both outcomes are visible in the audit log. Manual intervention is the right resolution path; the reconciler does not silently rebind across facility codes.

## 11. Access policy handling

> **[SUPERSEDED — see [Correction (2026-06-29)](#correction-2026-06-29--read-path-no-longer-uses-nfc_id-supersedes-8-9-10s-read-claims-and-11)].** The "use the first of `access_policy_ids`" rule below was replaced by managed-policy filtering: `UnifiClient` takes `managed_policy_ids` (the `tier_mapping` targets), reads only the managed policy (ignoring policies UniFi auto-applies to all users), and writes only the tier policy. The text below is retained for historical context.

Each `ResolvedMember.target_policy` is a single string — the UniFi policy UUID. We push it as `{"access_policy_ids": [target_policy]}` (single-element list).

When fetching, UniFi may return multiple policies per user (`access_policy_ids: [...]`). The reconciler:

- Uses the **first** `access_policy_ids[0]` as `UnifiUser.policy`.
- If `len(access_policy_ids) > 1`, emits a warning at fetch time (covered by §7).

This treats "user has policies A + B in UniFi but CiviCRM says target=A" as a no-op (policy A is already there) — matching the architecture's diff semantics where `to_update_policy` fires only when `u.policy != r.target_policy`. The downside: the warning is operational drift the operator may want to clean up manually, but the reconciler doesn't enforce single-policy because UniFi supports multi-policy as a normal configuration.

## 12. Retries and error surface

Identical retry helper shape to `CivicrmClient._with_retries`:

- **Max attempts:** 3 (initial + 2 retries)
- **Backoff:** exponential — 1s, then 2s; ±20% jitter
- **Retry triggers:** `httpx.RequestError`, HTTP 5xx, HTTP 429
- **No retry on:** HTTP 4xx other than 429 (including the non-standard `402`, "Request Failed"), JSON parse errors, `code != "SUCCESS"` in the envelope
- **On 429:** `Retry-After` header in seconds → sleep that long; else fall back to exponential
- **After exhaustion:** raise `UnifiClientError` with attempt count, last HTTP status, last response body (truncated to 200 chars), and originating exception type

`UnifiClientError` is the only public exception. The orchestrator does not catch (architecture §11); the scheduler's per-cycle `try/except` does.

## 13. Test plan

`tests/test_unifi_client.py` (via `pytest-httpx`):

**Construction / TLS:**

1. `test_init_verifies_tls_fingerprint_match` — stub `socket.create_connection` + `ssl.SSLContext.wrap_socket` to return a fake cert whose SHA-256 matches `config.tls_fingerprint`. Client constructs successfully.
2. `test_init_raises_on_tls_fingerprint_mismatch` — stub returns a cert whose hash differs. Expect `UnifiClientError("TLS fingerprint mismatch…")`. No `httpx.Client` should be created.
3. `test_context_manager_closes_http_client` — using `with UnifiClient(...) as c:` closes `c._http`.

**`fetch_users`:**

4. `test_fetch_users_happy_path` — one page, returns `list[UnifiUser]` with parsed `contact_id`, `display_name`, `card_id`, `active`, `policy`.
5. `test_fetch_users_paginates` — 101 users across 2 pages; client follows until `len(page) < page_size`.
6. `test_fetch_users_skips_admin_without_employee_number` — user with `employee_number=""` is omitted.
7. `test_fetch_users_skips_non_int_employee_number` — `employee_number="bob"` is omitted.
8. `test_fetch_users_logs_warning_on_multiple_cards` — user with two cards; the first is used; one warning logged.
9. `test_fetch_users_redacts_card_id_in_logs` — warning message contains `****1234`, never the full card_id.

**`apply` (dry-run):**

10. `test_apply_dry_run_makes_no_writes` — non-empty diff; assert zero httpx writes; each intended action logged as `would-…` with redacted card.
11. `test_apply_dry_run_still_fetches_token_map` — confirm the read for `/nfc_cards/tokens` happens in dry-run when the diff has card changes (so the dry-run report accurately reflects what would be imported).

**`apply` (live):**

12. `test_apply_requires_prior_fetch_users` — calling `apply()` first raises `UnifiClientError`.
13. `test_apply_executes_deactivate_then_update_then_add_order` — diff with one entry in each bucket; assert HTTPX call sequence.
14. `test_apply_imports_unknown_card_then_binds` — `to_add` with a `card_id` not present in the token map → one POST to `/credentials/nfc_cards/import`, then PUT to `/users/:id/nfc_cards` with the returned token.
15. `test_apply_reuses_existing_token_for_known_card` — `card_id` already in token map → no import POST; direct PUT bind.
16. `test_apply_create_new_user_path` — `to_add` for an unknown `contact_id` → POST `/users`, capture returned `id`, bind card, assign policy.
17. `test_apply_reactivate_inactive_user_path` — `to_add` for a `contact_id` cached as inactive (and same card_id as before) → PUT `/users/:id` with `status: "ACTIVE"`, bind card, assign policy. No DELETE of old card.
17a. `test_apply_reactivate_swaps_card_when_changed` — `to_add` for a cached-inactive contact whose previous cached card differs from `resolved.card_id` → PUT activate, DELETE old card via token, PUT bind new card, PUT assign policy. Order matters; assert sequence.
18. `test_apply_deactivate_sets_status` — `to_deactivate` issues PUT with `{"status": "DEACTIVATED"}`.
19. `test_apply_update_credential_swaps_card` — DELETE old card, PUT new card.
20. `test_apply_update_policy_replaces` — PUT `/users/:id/access_policies` with the single target policy.
21. `test_apply_inter_call_delay` — patch `time.sleep`; assert called `len(writes) - 1` times with `0.075`.
22. `test_apply_card_import_failure_raises` — import response has `token: ""` for one row → `UnifiClientError`.
22a. `test_apply_imports_use_2col_csv_format` — assert the multipart body uploaded to `/credentials/nfc_cards/import` contains lines of the form `<nfc_id>,sync-<zero-padded-card_id>` with no header row, per the format verified in §9.

**HTTP / envelope:**

23. `test_non_success_envelope_raises` — server returns 200 with `{"code": "CODE_AUTH_FAILED", "msg": "Authentication failed."}` → `UnifiClientError("CODE_AUTH_FAILED: …")`.
24. `test_http_500_retries_then_raises` — three 500s → `UnifiClientError`, three calls made.
25. `test_http_429_honors_retry_after_seconds` — first response 429 with `Retry-After: 5`; second 200. Patch `time.sleep`; assert it was called with ≥5.
26. `test_http_402_raises_immediately_no_retry` — 402 is non-retryable.
27. `test_malformed_json_raises` — body isn't JSON → `UnifiClientError`.

**Card-ID conversion:**

28. `test_compute_nfc_id_known_values` — `_compute_nfc_id(42, 1234) == "2A04D2"`, `_compute_nfc_id(42, 1235) == "2A04D3"` (illustrative encodings from §10).
29. `test_compute_nfc_id_zero_card_number` — `_compute_nfc_id(42, 0) == "2A0000"`.
30. `test_parse_nfc_id_matching_facility_code` — `_parse_nfc_id("2A04D2", 42) == 1234`, `_parse_nfc_id("2A04D3", 42) == 1235`.
31. `test_parse_nfc_id_mismatched_facility_code_returns_none` — `_parse_nfc_id("2A04D2", 99) is None`.
32. `test_parse_nfc_id_garbage_returns_none` — `_parse_nfc_id("not-hex", 42) is None`.
33. `test_parse_nfc_id_lowercase_hex_still_parses` — `_parse_nfc_id("2a04d3", 42) == 1235` (defensive: don't trust UniFi to always uppercase).

**Name splitting:**

30. `test_split_name_two_words` — `"Jane Doe"` → `("Jane", "Doe")`.
31. `test_split_name_three_words_splits_on_last_space` — `"Mary Anne Doe"` → `("Mary Anne", "Doe")`.
32. `test_split_name_single_word_pads_last_name` — `"Madonna"` → `("Madonna", "—")` (placeholder since UniFi requires both on create).
33. `test_split_name_empty_string_raises` — display_name should always be present; defensive raise.

## 14. Config changes

Add `facility_code` to `UnifiConfig`:

```python
@dataclass(frozen=True)
class UnifiConfig:
    host: str
    api_key: str
    tls_fingerprint: str
    facility_code: int
```

Validator changes in `_validate_unifi`:

- New: read `facility_code` from the `[unifi]` TOML table. Must be an int in `0..255`. Default: no default — operator must set it explicitly (the value is site-specific and getting it wrong silently binds cards to wrong users in adjacent facility-code spaces).

Documentation in `config.example.toml`:

```toml
[unifi]
host = "192.168.1.1"
tls_fingerprint = "AA:BB:CC:DD:EE:FF:..." # SHA-256 of the controller cert
# Wiegand 26-bit facility code (0-255), constant per site.
# Get this from your access-control vendor or by reading any existing
# enrolled card via the UniFi Access UI > Credentials > NFC Cards.
facility_code = 42
```

Test changes: extend `_write_minimal_valid` in `tests/test_config.py` to include `facility_code`. Update happy-path and drift tests accordingly.

## 15. Risks

- **TLS fingerprint pinning is once-per-cycle, not once-per-connection.** Documented in §6. Threat model is LAN-local; mitigation paths (custom HTTPTransport) noted.
- **`employee_number` is `String` at UniFi but `int` in our domain.** Round-trip via `str(contact_id)` on write, `int(employee_number)` on read with skip-on-failure. Tested in §13.
- **Implicit ordering: `apply()` requires prior `fetch_users()` on the same instance.** Mitigation: explicit precondition check raises `UnifiClientError`, tested in §13.
- **The 6.19 endpoint requires UniFi Access v3.3.10+.** Mitigation: documented in `config.example.toml` comment; runtime failure would surface as a 4xx with a clear message.
- **Multiple cards or policies per user are tolerated but warned about.** Mitigation: log redacted operational warnings; reconciler does not auto-correct because UniFi supports multi-card/multi-policy natively.
- **`facility_code` misconfiguration silently binds cards under the wrong site.** Mitigation: required (no default), documented retrieval path in `config.example.toml`.
- **Multipart CSV upload is one request type we haven't used elsewhere.** Mitigation: httpx supports multipart natively (`files={"file": ("cards.csv", csv_bytes, "text/csv")}`); tested via `pytest-httpx` like every other request. The end-to-end shape (auth, content-type, field name, body format) was confirmed working against the real controller during design verification.
- **No idempotency canary at the UniFi-client level.** The reconciler-side canary (`test_reconciler.py`) is the canonical check. Adding one here would require simulating the entire UniFi API in a fake — outside this slice's scope.

## 16. Things explicitly NOT decided here

- **Page size of 100.** Reasonable for the user-list and card-token-list endpoints; could be tuned. Not a config knob in v1.
- **Retry budget of 3 / inter-call delay of 75ms.** Both are constants in `client.py`. Promotable to config fields if real-world tuning demands.
- **Whether to compress the CSV upload.** Not done; CSVs are kilobyte-scale.
- **First-name placeholder for single-word display names.** `"—"` (em-dash) — chosen to be visibly distinct in the UniFi UI so an operator notices and edits the CiviCRM record. Open to bikeshed.
- **Whether `force_add: true` should ever be used.** Still `false` — door-sync never steals a card from another user. Recycled physical cards are instead freed at their *source*: deactivating a member now deletes their card (step 3), so the number is available the next time it's assigned. When a card is nonetheless still bound elsewhere (a member deactivated before this behavior shipped, or a manually-enrolled admin card), the bind fails as a per-user error that names the current holder, so an operator can free it deliberately rather than door-sync force-rebinding. A blanket `force_add: true` remains rejected because it would also clobber admin-managed bindings.
- **Audit-log entries for UniFi calls.** Out of scope; `audit.py` (a separate slice) owns audit logging. The orchestrator passes the diff and the result there.
