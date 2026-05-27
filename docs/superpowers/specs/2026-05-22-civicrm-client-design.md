# CiviCRM client — design

**Date:** 2026-05-22
**Status:** Approved for planning
**Companion:** [`docs/architecture.md`](../../architecture.md) §4 (module table — `civicrm.client`), §7 step 1 (the data we need), §10 (orchestrator integration), §11 (HTTP/error conventions). This spec also closes architecture §12's "CiviCRM client API surface" deferral.

---

## 1. Goal

Implement the read-only CiviCRM API4 client that the orchestrator will call once per reconcile cycle to get the current set of door-eligible members.

When this slice ships:
- `CivicrmClient(config.civicrm).fetch_active() -> list[CiviMember]` works end-to-end against a WordPress-hosted CiviCRM API4 endpoint
- Pagination, retries, and the join from `Contact` → `Membership` are handled inside the client
- The `card_id` source field is configurable per deployment
- Tests cover happy path, pagination, error paths, and retry behavior via `pytest-httpx`

## 2. Definition of done

All three commands green:

```bash
uv run pytest
uv run mypy --strict src tests
uv run ruff check .
```

Plus:
- `src/door_sync/civicrm/__init__.py` and `src/door_sync/civicrm/client.py` exist
- `tests/test_civicrm_client.py` exists with at least the 12 tests listed in §11
- `CivicrmConfig` gains a `card_id_field: str` attribute, validated in `_validate_civicrm`
- `config.example.toml` documents the new field
- `pytest-httpx` is added as a dev dependency
- The CiviCRM client uses sync `httpx` only — no asyncio, no streaming
- `CivicrmClient` exposes one public method (`fetch_active`), one public exception (`CivicrmClientError`), and a context-manager protocol for resource cleanup

## 3. Non-goals (deferred)

- **Write paths.** CiviCRM client is read-only by design. Updates to CiviCRM (if ever needed) get their own slice.
- **Retry of the whole `fetch_active` operation.** Individual HTTP calls retry; the operation as a whole does not — the scheduler's per-cycle `try/except` handles that level (architecture §3).
- **Async / streaming.** Sync `httpx` per architecture §3.
- **Caching.** Every `fetch_active()` hits the API fresh.
- **Alternate auth schemes** (OAuth, basic auth, separate site_key). Single combined `Bearer api_key` per this deployment's setup.
- **Standalone CLI.** The orchestrator/main slice wires this up.
- **`Retry-After` HTTP-date format.** Only the seconds form is handled. (Almost no CiviCRM deployment uses HTTP-date for `Retry-After`.)

## 4. The class

```python
class CivicrmClient:
    def __init__(self, config: CivicrmConfig) -> None: ...
    def fetch_active(self) -> list[CiviMember]: ...
    def close(self) -> None: ...
    def __enter__(self) -> "CivicrmClient": ...
    def __exit__(self, *args: object) -> None: ...


class CivicrmClientError(Exception):
    """Raised on any non-recoverable CiviCRM API failure."""
```

One internal `httpx.Client` instance, created in `__init__`, closed in `close()`. The class is constructed per reconcile cycle (architecture §10) — no globals, no module-level singletons.

The constructor signature takes the whole `CivicrmConfig` slice rather than individual fields. If `CivicrmConfig` gains optional fields later (e.g., custom timeout), the client doesn't need a new keyword argument.

## 5. HTTP contract

- **URL pattern:** `POST <host>/civicrm/ajax/api4/<Entity>/<Action>`
- **Auth:** `Authorization: Bearer <api_key>` header, `X-Requested-With: XMLHttpRequest`
- **Body:** `application/x-www-form-urlencoded`, with a single `params` field containing JSON-encoded query parameters (this is the standard CiviCRM API4 over REST convention)
- **Response:** JSON with a `values` array of result records and an optional `count` total
- **httpx config:** `base_url=config.host`, `timeout=httpx.Timeout(connect=10.0, read=30.0)`, `verify=True`, no following redirects (CiviCRM REST doesn't redirect)

**Verification step during implementation:** Before the full test suite is written, the implementer should make one manual `curl` (or `httpx`-from-REPL) call against the real prod CiviCRM endpoint to confirm the auth header and body encoding. If the deployment uses a different convention (e.g., `api_key` as a query parameter, or pure JSON body), adjust §5 and §6 to match before writing the tests. The shape choices documented here are the most common for WP-hosted API4 but are NOT universal. See §14 risks.

The exact body shape per request:

```python
data = {"params": json.dumps({
    "select": ["id", "display_name", config.card_id_field],
    "where": [
        [config.card_id_field, "IS NOT EMPTY"],
        ["is_deleted", "=", False],
    ],
    "limit": 250,
    "offset": N,
})}
```

`json` and `httpx` are stdlib + existing dependency respectively.

## 6. `fetch_active` flow

1. **Contacts query** (one or more pages of `Contact.get`):
   - Select: `id`, `display_name`, `<card_id_field>`
   - Where: `<card_id_field> IS NOT EMPTY` AND `is_deleted = false`
   - Paginate at 250 per page; follow until a page returns fewer than 250 records
2. **Memberships query** (one or more pages of `Membership.get`):
   - Select: `contact_id`, `membership_type_id:label`, `status_id:name`
   - Where: `contact_id IN [<all_contact_ids_from_step_1>]` AND `status_id:name IN ["Current", "Grace"]`
   - Paginate similarly
3. **Join in Python**: build `dict[int, list[str]]` keyed by `contact_id`, value = list of distinct membership type labels
4. **Construct `CiviMember`s**: one per contact, with `card_id` from the configured field and `membership_types` from the join (empty list if no active memberships)
5. **Return** the list

If step 1 returns zero contacts, skip step 2 entirely and return `[]`.

**Why no card_id contacts are filtered out in step 1, not in Python:** the `where` clause does this server-side so we never pay to transfer them. The `IS NOT EMPTY` operator in API4 covers both NULL and empty-string cases.

**Why members with no active memberships are kept in the result:** their `CiviMember.membership_types = []` resolves to `"unmapped"` in `tier_mapping`, which the `safety` guard halts on. This is the right behavior — a card without an active membership is a flag worth surfacing, not silently ignoring.

**Why `is_deleted = false` is included:** CiviCRM soft-deletes contacts to a trash bin. Without this filter, deleted contacts with card_ids would appear in results and get door access. (Most queries naturally exclude trashed records but `IS NOT EMPTY` on a custom field does not.)

## 7. Retries

Internal helper that wraps each HTTP call:

- **Max attempts:** 3 (initial + 2 retries)
- **Backoff:** exponential — 1s, then 2s; plus small jitter (±20%) to avoid thundering herd
- **Retry triggers:** `httpx.RequestError` (network/transport), HTTP 5xx status, HTTP 429
- **No retry on:** HTTP 4xx (other than 429), JSON parse errors
- **On 429:** read `Retry-After` header. If integer-seconds form, sleep that long. If HTTP-date or absent, fall back to exponential backoff
- **After exhaustion:** raise `CivicrmClientError` wrapping the final exception or response

The retry helper is private (`_with_retries` or similar) and lives in `client.py`. It does not retry the entire `fetch_active` — only individual HTTP requests.

## 8. Error surface

`CivicrmClient` raises `CivicrmClientError` (and only `CivicrmClientError`) when something goes wrong, with a message that includes:
- The attempt count
- The last HTTP status code (if applicable)
- The last response body snippet (truncated to 200 chars to avoid log spam)
- The original exception type and message (if applicable)

Architecture §11: "Clients: raise on HTTP errors after exhausting retries. The orchestrator does not catch; the scheduler catches and continues." We follow that exactly.

No retries on `CivicrmClientError` itself — once it's raised, the scheduler's per-cycle try/except logs it and moves on to the next cycle.

## 9. Config changes

Add `card_id_field` to `CivicrmConfig`:

```python
@dataclass(frozen=True)
class CivicrmConfig:
    host: str
    api_key: str
    card_id_field: str
```

Validator change in `_validate_civicrm`: require non-empty string, no internal whitespace. The CiviCRM API4 custom-field format is `CustomGroupName.field_name` (dot-separated), but we don't enforce the dot — some deployments use single-word `external_identifier`.

Documentation change in `config.example.toml`:

```toml
[civicrm]
host = "https://civicrm.example.org"
# CiviCRM API4 custom field on the Contact entity holding the access card ID.
# Format: "CustomGroupName.field_name" (e.g. "Door_Access.card_id").
card_id_field = "Door_Access.card_id"
```

Test changes: extend `_write_minimal_valid` in `tests/test_config.py` to include `card_id_field`. Update `test_load_happy_path_returns_populated_config` to assert it. Update the drift test to assert the example file's value.

## 10. Dev dependency

Add `pytest-httpx>=0.30` to `[dependency-groups].dev` in `pyproject.toml`. Run `uv sync` to install.

`pytest-httpx` provides a `httpx_mock` fixture that intercepts httpx calls and lets tests register canned responses declaratively. Cleaner than monkeypatching for our test volume.

## 11. Test plan

`tests/test_civicrm_client.py`:

1. **`test_fetch_active_happy_path`** — single page of contacts, single page of memberships, returns one `CiviMember` per contact with the right `membership_types`.
2. **`test_fetch_active_paginates_contacts`** — 251 contacts spread across 2 pages of 250 + 1; the client follows offset until short page.
3. **`test_fetch_active_paginates_memberships`** — similar, on the memberships query.
4. **`test_fetch_active_empty_result`** — zero contacts → returns `[]`, no memberships query made.
5. **`test_contact_with_multiple_active_memberships`** — one contact has both `Gold` (Current) and `Comp` (Current); `CiviMember.membership_types` contains both.
6. **`test_contact_with_no_active_membership_kept_with_empty_types`** — contact has a card_id, no Current/Grace membership; appears in result with `membership_types=[]`.
7. **`test_expired_memberships_filtered`** — contact has one `Expired` and one `Grace` membership; only `Grace`-status type appears in `membership_types`.
8. **`test_http_401_raises_no_retry`** — `httpx_mock` returns 401 on first call; expect `CivicrmClientError`, expect only one HTTP request was made.
9. **`test_http_500_retries_then_raises`** — three 500 responses → `CivicrmClientError`, three requests made.
10. **`test_http_500_then_200_succeeds`** — first attempt 500, second attempt 200; returns the data.
11. **`test_http_429_honors_retry_after_seconds`** — first response is 429 with `Retry-After: 5`; second is 200. Use `monkeypatch.setattr("time.sleep", mock_sleep)` where `mock_sleep` is a `list.append`-based recorder. After `fetch_active()` succeeds, assert that `mock_sleep` was called with a value `>= 5` at least once. This makes the test instant and asserts the wait was respected without actually sleeping.
12. **`test_malformed_json_raises`** — server returns 200 with body that isn't valid JSON; expect `CivicrmClientError`.
13. **`test_context_manager_closes_http_client`** — using the client as a context manager calls the underlying httpx.Client's close. Verify via `hasattr(client._http, "is_closed")` or by attempting a second call.

(That's 13 — one above the bar set in §2.)

Tests use realistic CiviCRM API4 response shapes:

```json
{
  "values": [
    {"id": 42, "display_name": "Jane Doe", "Door_Access.card_id": "12345"}
  ],
  "count": 1
}
```

For membership responses, the joined fields use API4's `:label` and `:name` suffixes:

```json
{
  "values": [
    {"contact_id": 42, "membership_type_id:label": "Gold", "status_id:name": "Current"}
  ]
}
```

## 12. Retry-helper implementation note

The retry helper takes a callable that returns an `httpx.Response` (or raises). Pseudo:

```python
def _with_retries(self, action: Callable[[], httpx.Response]) -> httpx.Response:
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = action()
            if response.status_code == 429:
                wait = _parse_retry_after(response) or _backoff_seconds(attempt)
                time.sleep(wait)
                continue
            if 500 <= response.status_code < 600:
                if attempt == _MAX_ATTEMPTS:
                    raise CivicrmClientError(...)
                time.sleep(_backoff_seconds(attempt))
                continue
            return response
        except httpx.RequestError as e:
            if attempt == _MAX_ATTEMPTS:
                raise CivicrmClientError(...) from e
            time.sleep(_backoff_seconds(attempt))
    # Unreachable, but required for mypy:
    raise CivicrmClientError("retry loop exited unexpectedly")
```

`time.sleep` in tests is patched via `pytest-httpx` or `monkeypatch`. Jitter (±20%) is added inside `_backoff_seconds`.

## 13. Things explicitly NOT decided here

- **Pagination size of 250.** Reasonable for CiviCRM but could be tuned. Not a config knob in v1 — change in code if needed.
- **Whether to compress requests.** Not done; CiviCRM responses are small (a few KB per page).
- **Connection pooling across cycles.** Not done; each cycle gets its own `httpx.Client`, which is the architecturally-intended behavior.
- **Whether `display_name` could be empty.** CiviCRM normally generates a display name from first+last; we treat whatever the API returns as the truth. If it's `""`, the downstream `UnifiUser` will have `display_name=""` until corrected in CiviCRM. Not the client's job to invent.

## 14. Risks

- **API4 auth and body encoding vary across deployments.** The exact headers (`Authorization: Bearer` vs `X-Civi-Auth: Bearer` vs query-param `api_key`) and body format (form-encoded `params=<json>` vs pure JSON body) depend on how the CiviCRM REST plugin is configured. Mitigation: the implementer performs one manual call against prod before writing tests, and adjusts §5–§6 to match the real shape. The 13 tests can then be written against the verified shape.
- **API4 join syntax could differ slightly across CiviCRM minor versions.** Mitigation: we use the simpler two-query approach (no joins in the request); the in-Python join is version-agnostic.
- **`where` operators (`IS NOT EMPTY`, `IN`) and pseudoconstant syntax (`status_id:name`) are API4-specific.** Mitigation: these are documented in the CiviCRM API Explorer; the implementer should sanity-check against the explorer (or one manual call) before writing the contacts/memberships query helpers.
- **The `card_id_field` value is deployment-specific and not validated against the actual schema.** Mitigation: it's a config value; misconfiguration produces a clear runtime error (CiviCRM returns "unknown field" 4xx); the operator fixes the config.
- **`pytest-httpx` is a new dev dep.** Mitigation: it's well-maintained and httpx-native; adds no runtime risk. Versions are pinned in `pyproject.toml`.
- **Retry behavior is hand-rolled rather than via `tenacity` or `httpx-retries`.** Mitigation: the retry logic is small (~30 lines), tested directly, and easier to read than configuring a third-party retry library. If we later need richer retry semantics (circuit breakers, distributed retries), revisit.
