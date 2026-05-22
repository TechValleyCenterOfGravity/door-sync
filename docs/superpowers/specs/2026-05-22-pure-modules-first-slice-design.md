# Pure-modules first slice — design

**Date:** 2026-05-22
**Status:** Approved for planning
**Companion:** [`docs/architecture.md`](../../architecture.md) is authoritative for module shape, data contracts, and the pure/impure boundary. This spec defers to it on every architectural question.

---

## 1. Goal

Bring the four pure modules of `door-sync` from empty skeleton to fully implemented and tested:

- `src/door_sync/models.py`
- `src/door_sync/tier_mapping.py`
- `src/door_sync/reconciler.py`
- `src/door_sync/safety.py`

When this slice ships, the entire correctness story of the eventual daemon (architecture §5) is covered by deterministic, I/O-free tests. Every later slice (clients, orchestrator, scheduler, etc.) builds on a verified foundation.

## 2. Definition of done

All three commands green from a clean checkout:

```bash
uv run pytest
uv run mypy --strict src tests
uv run ruff check .
```

Plus:

- Every row of the architecture §8 diff truth table is covered by a named test in `tests/test_reconciler.py`.
- Every row of the architecture §9 safety guard table is covered by a named test in `tests/test_safety.py`.
- The **idempotency canary** test exists and passes: `compute_diff` run against a `UnifiUser` list that has had a `Diff` hand-projected onto it produces an all-empty `Diff`.
- No I/O imports (no `httpx`, no `logging`, no file or env access) appear in the four pure-module files.
- One commit per module, in dependency order: `models` → `tier_mapping` → `reconciler` → `safety`.

## 3. Non-goals (deferred to later slices)

- No `config.py` — no env loading, no TOML parsing. Tests construct mapping and thresholds directly.
- No clients — `civicrm/`, `unifi/` remain unimplemented.
- No `orchestrator`, `scheduler`, `audit`, `state`, or `alert` modules.
- No CLI flag plumbing — `__main__.py` stays the existing stub.
- No `tests/fixtures/` directory (architecture §11 reserves it for client tests, which aren't in this slice).
- No real CiviCRM or UniFi API contact of any kind.

## 4. Module-by-module design

### 4.1 `models.py`

Contains every frozen dataclass from architecture §6, plus three new ones this slice introduces.

**Verbatim from architecture §6** (no changes):

- `CiviMember(contact_id, display_name, card_id, membership_types)`
- `ResolvedMember(contact_id, display_name, card_id, target_policy, resolution)`
- `UnifiUser(contact_id, display_name, card_id, active, policy)`
- `Diff(to_add, to_update_credential, to_update_policy, to_deactivate, unmapped)`
- `CheckResult(halted, reason)`
- `ReconcileResult(halted, reason, diff)`

**New in this slice** (architecture §6 leaves the mapping/thresholds shape implicit; defining them here lets `tier_mapping` and `safety` take single, typed arguments):

```python
@dataclass(frozen=True)
class TierRule:
    resolution: Literal["tier", "none", "day-pass"]
    target_policy: str | None  # required when resolution == "tier"; None otherwise
    rank: int                  # used for highest-wins on multi-membership contacts

@dataclass(frozen=True)
class TierMapping:
    rules: dict[str, TierRule]  # keyed by CiviCRM membership-type name

@dataclass(frozen=True)
class SafetyThresholds:
    mass_deactivate_pct: float = 0.15
    mass_add_pct: float = 0.25
    mass_policy_pct: float = 0.20
    baseline_floor: int = 10
```

`"unmapped"` is intentionally **not** a `TierRule.resolution` value. Unmapped = the *absence* of a rule for some membership type the member holds. This keeps the rule table tight and makes "did we configure this type?" a single dict-membership check.

All dataclasses are `@dataclass(frozen=True)`. Mutable containers inside frozen dataclasses (`list`, `dict`) are allowed but conceptually immutable — never mutated in place; construct a new dataclass with the updated value (architecture §11).

### 4.2 `tier_mapping.py`

Pure. Imports `models` only. No logging, no config lookups, no exceptions on data issues (architecture §5).

```python
def resolve(member: CiviMember, mapping: TierMapping) -> ResolvedMember
def resolve_all(members: list[CiviMember], mapping: TierMapping) -> list[ResolvedMember]
```

`resolve` semantics:

1. If `member.membership_types` is empty → return `resolution="unmapped"`, `target_policy=None`. (A member with no types can't be assigned a tier; treat as a configuration gap.)
2. If **any** type in `member.membership_types` is missing from `mapping.rules` → return `resolution="unmapped"`, `target_policy=None`. Fail-secure: one unknown type halts the cycle via the §9 unmapped guard rather than silently using whichever rules happened to match.
3. Otherwise, among the matched rules, pick the one with the highest `rank`. That rule's `resolution` and `target_policy` populate the returned `ResolvedMember`. Ties on rank are not expected in valid configs; behavior on tie is "pick one deterministically" — implementation will sort by rank descending then by membership-type name, taking the first.

`resolve_all` is a list comprehension; it exists because architecture §7 step 2 calls a `resolve_all`-shaped operation on the whole CiviCRM list.

### 4.3 `reconciler.py`

Pure. Imports `models` only.

```python
def compute_diff(resolved: list[ResolvedMember], unifi: list[UnifiUser]) -> Diff
```

Implementation follows architecture §8 verbatim:

1. Index `resolved` by `contact_id` (dict).
2. Index `unifi` by `contact_id` (dict).
3. Iterate the union of `contact_id` keys. For each, consult the §8 truth table and append to the appropriate `Diff` list (or no-op).
4. Return a `Diff` constructed once at the end (frozen dataclass — built from local lists, then handed off).

Edge case the §8 table doesn't enumerate: `resolved` has `resolution="tier"` but `target_policy is None`. This is malformed input — `tier_mapping.resolve` should never produce it. Treat as a no-op (skip the contact) rather than raising; the type system upstream is what prevents this in practice.

### 4.4 `safety.py`

Pure. Imports `models` only.

```python
def check(diff: Diff, *, baseline: int, thresholds: SafetyThresholds) -> CheckResult
```

Guards run in this order (first to fire wins, since any single trigger halts the cycle anyway per architecture §9):

1. **Unmapped types** — `len(diff.unmapped) > 0` → halt.
2. **Duplicate card IDs** — collect `card_id` from each `ResolvedMember` in `diff.to_add` and from the `ResolvedMember` half of each `(resolved, unifi)` tuple in `diff.to_update_credential`; if any two non-None `card_id`s in that collected list are equal → halt. (Architecture §9 says "any two `ResolvedMember`s share a non-None `card_id`" — `safety.check`'s signature only gives access to the diff, so the practical scope is "resolved members the diff is about to write." Members the diff isn't touching aren't a write hazard.)
3. **Invalid card ID** — using the same collected `card_id` list from guard 2: any value outside `0..65535` → halt.
4. **Mass deactivation** — `baseline >= floor` and `len(diff.to_deactivate) / baseline > mass_deactivate_pct` → halt.
5. **Mass addition** — `baseline >= floor` and `len(diff.to_add) / baseline > mass_add_pct` → halt.
6. **Mass policy change** — `baseline >= floor` and `len(diff.to_update_policy) / baseline > mass_policy_pct` → halt.

If no guard fires, return `CheckResult(halted=False, reason=None)`.

The `reason` string for each halt is a stable, human-readable sentence — used by `audit.log_halt` and `alert.send` in later slices. Format: a short noun phrase + the relevant numbers. Example: `"mass deactivation: 23 of 87 active users (26.4%) exceeds 15.0% threshold"`. Exact wording is an implementation detail but should include enough context for an operator reading the alert to know which guard fired and why.

## 5. Test plan

One test file per module, mirroring source layout (architecture §11).

### 5.1 `tests/test_models.py`

- Each dataclass is frozen: attempting to assign an attribute raises `FrozenInstanceError`.
- Construction round-trips: a hand-built instance equals itself.
- (No behavioral logic to test in `models`; this file is a sanity check.)

### 5.2 `tests/test_tier_mapping.py`

Table-driven cases:

- Single tier match → `resolution="tier"`, correct `target_policy`.
- Single `none` match → `resolution="none"`, `target_policy=None`.
- Single `day-pass` match → `resolution="day-pass"`, `target_policy=None`.
- Member has type missing from mapping → `resolution="unmapped"`.
- Member with empty `membership_types` → `resolution="unmapped"`.
- Highest-wins, two `tier` rules → higher-rank rule's `target_policy` chosen.
- Highest-wins across resolution kinds → highest-rank rule wins regardless of its kind.
- Mixed: one matched type + one unmatched type → `unmapped` (fail-secure beats highest-wins).
- `resolve_all` returns one `ResolvedMember` per input, in order.

### 5.3 `tests/test_reconciler.py`

One named test per row of architecture §8:

1. `tier` resolution, not present in UniFi → `to_add`.
2. `tier` resolution, present + active, `card_id` differs → `to_update_credential`.
3. `tier` resolution, present + active, `display_name` differs → `to_update_credential`.
4. `tier` resolution, present + active, `policy` differs → `to_update_policy`.
5. `tier` resolution, present + active, no differences → no-op.
6. `tier` resolution, present + inactive → `to_add` (reactivate path).
7. `none` resolution, present + active → `to_deactivate`.
8. `none` resolution, present + inactive → no-op.
9. `none` resolution, not present → no-op.
10. `day-pass` resolution, any UniFi state → no-op.
11. `unmapped` resolution → appended to `Diff.unmapped`, no other diff sets.
12. Not in resolved, present + active → `to_deactivate`.
13. Not in resolved, present + inactive or not present → no-op.

Plus one combined test: `tier` resolution with both `card_id` and `policy` different → entry appears in both `to_update_credential` and `to_update_policy` (architecture §8 last paragraph).

Plus the **idempotency canary**:

```python
def test_idempotency_canary() -> None:
    resolved = [...]
    unifi = [...]
    diff = compute_diff(resolved, unifi)
    new_unifi = apply_diff_in_memory(diff, unifi)  # test helper
    second_diff = compute_diff(resolved, new_unifi)
    assert second_diff == Diff(
        to_add=[], to_update_credential=[], to_update_policy=[],
        to_deactivate=[], unmapped=[],
    )
```

`apply_diff_in_memory` lives in the test file (not in production code). It takes a `Diff` and a `UnifiUser` list and returns a new list with `to_add` appended as active users, `to_update_*` field changes applied, and `to_deactivate` flipped to `active=False`. It's a faithful in-memory model of what the eventual `UnifiClient.apply()` will do — the canary fails if the diff algorithm and the projection disagree.

### 5.4 `tests/test_safety.py`

One named test per guard, plus boundary cases:

- Clean diff returns `halted=False`.
- Unmapped non-empty → halted, reason mentions "unmapped".
- Two `to_add` entries with same `card_id` → halted, reason mentions "duplicate".
- One `to_add` with `card_id=-1` and one with `card_id=70000` → each halts independently in its own test.
- Mass deactivation at 15.01% with baseline above floor → halted.
- Mass deactivation at 14.99% with baseline above floor → not halted.
- Mass addition over threshold → halted.
- Mass policy change over threshold → halted.
- Baseline below floor + mass-deactivation count that *would* trip percentage → not halted (floor skip).
- Baseline below floor + unmapped non-empty → still halted (integrity guards ignore floor).

### 5.5 Test file cleanup

Delete `tests/test_smoke.py`. Its single assertion (`main() returns 0`) is no longer useful once real tests exist, and `__main__.main` will be rewritten in a later slice anyway.

## 6. Build order

Strict dependency order, one commit per module:

1. **`models.py`** + `tests/test_models.py` — no other module depends on tests passing here, but freezing the data contracts first prevents churn downstream.
2. **`tier_mapping.py`** + `tests/test_tier_mapping.py` — depends on `models`.
3. **`reconciler.py`** + `tests/test_reconciler.py` — depends on `models`. (Independent of `tier_mapping` at the type level; tests construct `ResolvedMember`s directly.)
4. **`safety.py`** + `tests/test_safety.py` — depends on `models`.

Steps 3 and 4 are independent and could be parallelized, but a single-developer sequential pass is simpler and the slice is small.

Each commit must pass `pytest`, `mypy --strict`, and `ruff check` before the next begins.

## 7. Things explicitly NOT decided here

These are deferred to later slices; mentioning them here so future-me doesn't re-litigate them inside this one.

- **Default `SafetyThresholds` values are baked in as dataclass defaults.** When `config.py` lands, configured values from TOML will override these per deployment (architecture §9 last paragraph). The defaults in this slice match architecture §9 verbatim.
- **`target_policy` strings are opaque** to the pure modules. They're treated as identifiers; whether they map to UniFi policy IDs, names, or something else is a UniFi-client concern.
- **`TierMapping.rules` is a plain `dict`**, not a `MappingProxyType`. Frozen dataclass + the architecture §11 "treat mutable containers as conceptually immutable" convention is sufficient discipline.
- **No property-based tests** (`hypothesis`) in this slice. The logic is small, deterministic, and table-driven; table-based examples are easier to read against the architecture doc. Property tests can be added later if a real bug motivates them.
- **No `apply_diff_in_memory` in production code.** It's a test helper, lives in `tests/test_reconciler.py`. Production application logic lives in the future `UnifiClient.apply()`.

## 8. Risks

- **The idempotency canary's `apply_diff_in_memory` could drift from the real `UnifiClient.apply()`.** Mitigation: when `unifi/client.py` is built, port the canary to also run against a fake `UnifiClient` so the test exercises the real apply path. Until then, the helper is the contract.
- **Highest-wins on rank ties is under-specified by the architecture doc.** This spec resolves it (sort by rank desc, then by type name asc). If a real-world mapping ever produces ties, revisit — the operator probably intended distinct ranks.
- **`safety.check` guard order is a judgment call.** Architecture §9 says "any single trigger halts" so order is observationally irrelevant for production, but it affects the `reason` string an operator sees. The chosen order (integrity guards before mass guards) means an operator reading a halt sees the most actionable cause first.
