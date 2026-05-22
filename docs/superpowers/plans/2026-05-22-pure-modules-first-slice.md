# Pure-modules first slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and test the four pure modules of `door-sync` (`models`, `tier_mapping`, `reconciler`, `safety`) so the entire correctness story of the eventual daemon is covered by deterministic, I/O-free tests.

**Architecture:** Four pure Python modules with frozen dataclasses and no I/O. Each is built TDD-style in dependency order: `models` (data contracts) → `tier_mapping` (resolution) → `reconciler` (diff algorithm) → `safety` (guard checks). Tests construct dataclasses by hand; no mocks, no fixtures, no HTTP. One commit per module.

**Tech Stack:** Python 3.11+, uv for env/scripts, pytest 9 for tests, mypy 2.1 (`--strict`) for type checking, ruff 0.15 for linting. No runtime dependencies in this slice (the `httpx` dep in `pyproject.toml` is for later slices).

**Spec:** [`docs/superpowers/specs/2026-05-22-pure-modules-first-slice-design.md`](../specs/2026-05-22-pure-modules-first-slice-design.md). Read it first.

**Conventions enforced by this slice (architecture §5, §11):**
- Pure modules import only `door_sync.models` (and stdlib). No `logging`, no `httpx`, no file or env access.
- All domain types are `@dataclass(frozen=True)`.
- Type hints on every function, including private ones. `mypy --strict` must be green.
- No `from x import *`. Imports ordered: stdlib → third-party → `door_sync.*`.

**Verification commands** (used in every task — same three each time):

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

All three must pass before moving to the next task. (The `uv run` invocation auto-syncs the venv if needed.)

---

## Task 1: Project hygiene + `models.py`

**Files:**
- Delete: `tests/test_smoke.py`
- Create: `tests/test_models.py`
- Modify: `src/door_sync/models.py` (currently does not exist — create it)

### Background

The existing `tests/test_smoke.py` asserts `__main__.main()` returns 0. Once real tests exist, this is noise — `__main__.py` will be rewritten in a later slice anyway. Delete it.

`models.py` contains all domain dataclasses. It imports only `dataclasses` and `typing` from stdlib. Every dataclass is `frozen=True`. No methods, no behavior — pure data.

- [ ] **Step 1.1: Delete the smoke test**

```bash
git rm tests/test_smoke.py
```

- [ ] **Step 1.2: Write `tests/test_models.py` — the full file**

```python
from dataclasses import FrozenInstanceError

import pytest

from door_sync.models import (
    CheckResult,
    CiviMember,
    Diff,
    ReconcileResult,
    ResolvedMember,
    SafetyThresholds,
    TierMapping,
    TierRule,
    UnifiUser,
)


def test_civi_member_is_frozen() -> None:
    m = CiviMember(
        contact_id=1, display_name="A", card_id=None, membership_types=[]
    )
    with pytest.raises(FrozenInstanceError):
        m.contact_id = 2  # type: ignore[misc]


def test_resolved_member_is_frozen() -> None:
    r = ResolvedMember(
        contact_id=1,
        display_name="A",
        card_id=None,
        target_policy=None,
        resolution="unmapped",
    )
    with pytest.raises(FrozenInstanceError):
        r.resolution = "tier"  # type: ignore[misc]


def test_unifi_user_is_frozen() -> None:
    u = UnifiUser(
        contact_id=1, display_name="A", card_id=None, active=True, policy=None
    )
    with pytest.raises(FrozenInstanceError):
        u.active = False  # type: ignore[misc]


def test_diff_is_frozen() -> None:
    d = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    with pytest.raises(FrozenInstanceError):
        d.to_add = []  # type: ignore[misc]


def test_check_result_is_frozen() -> None:
    c = CheckResult(halted=False, reason=None)
    with pytest.raises(FrozenInstanceError):
        c.halted = True  # type: ignore[misc]


def test_reconcile_result_is_frozen() -> None:
    rr = ReconcileResult(halted=False, reason=None, diff=None)
    with pytest.raises(FrozenInstanceError):
        rr.halted = True  # type: ignore[misc]


def test_tier_rule_is_frozen() -> None:
    t = TierRule(resolution="tier", target_policy="P1", rank=1)
    with pytest.raises(FrozenInstanceError):
        t.rank = 2  # type: ignore[misc]


def test_tier_mapping_is_frozen() -> None:
    m = TierMapping(rules={})
    with pytest.raises(FrozenInstanceError):
        m.rules = {}  # type: ignore[misc]


def test_safety_thresholds_defaults() -> None:
    t = SafetyThresholds()
    assert t.mass_deactivate_pct == 0.15
    assert t.mass_add_pct == 0.25
    assert t.mass_policy_pct == 0.20
    assert t.baseline_floor == 10


def test_safety_thresholds_is_frozen() -> None:
    t = SafetyThresholds()
    with pytest.raises(FrozenInstanceError):
        t.mass_add_pct = 0.5  # type: ignore[misc]


def test_dataclass_equality_round_trips() -> None:
    a = CiviMember(
        contact_id=1, display_name="A", card_id=42, membership_types=["X"]
    )
    b = CiviMember(
        contact_id=1, display_name="A", card_id=42, membership_types=["X"]
    )
    assert a == b
```

- [ ] **Step 1.3: Run pytest to verify failure**

Run: `uv run pytest tests/test_models.py -v`

Expected: collection error or ImportError — `door_sync.models` does not exist yet.

- [ ] **Step 1.4: Write `src/door_sync/models.py` — the full file**

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CiviMember:
    contact_id: int
    display_name: str
    card_id: int | None
    membership_types: list[str]


@dataclass(frozen=True)
class ResolvedMember:
    contact_id: int
    display_name: str
    card_id: int | None
    target_policy: str | None
    resolution: Literal["tier", "none", "day-pass", "unmapped"]


@dataclass(frozen=True)
class UnifiUser:
    contact_id: int
    display_name: str
    card_id: int | None
    active: bool
    policy: str | None


@dataclass(frozen=True)
class Diff:
    to_add: list[ResolvedMember]
    to_update_credential: list[tuple[ResolvedMember, UnifiUser]]
    to_update_policy: list[tuple[ResolvedMember, UnifiUser]]
    to_deactivate: list[UnifiUser]
    unmapped: list[ResolvedMember]


@dataclass(frozen=True)
class CheckResult:
    halted: bool
    reason: str | None


@dataclass(frozen=True)
class ReconcileResult:
    halted: bool
    reason: str | None
    diff: Diff | None


@dataclass(frozen=True)
class TierRule:
    resolution: Literal["tier", "none", "day-pass"]
    target_policy: str | None
    rank: int


@dataclass(frozen=True)
class TierMapping:
    rules: dict[str, TierRule]


@dataclass(frozen=True)
class SafetyThresholds:
    mass_deactivate_pct: float = 0.15
    mass_add_pct: float = 0.25
    mass_policy_pct: float = 0.20
    baseline_floor: int = 10
```

- [ ] **Step 1.5: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: pytest shows 11 passed; mypy shows "Success: no issues found"; ruff shows "All checks passed!".

If mypy complains about the `# type: ignore[misc]` comments being unused, that means the mypy version doesn't flag frozen-dataclass writes — remove the ignores. If mypy complains they're needed, leave them.

- [ ] **Step 1.6: Commit**

```bash
git add tests/test_models.py tests/test_smoke.py src/door_sync/models.py
git commit -m "Add domain dataclasses in models.py"
```

(The `git add` includes `tests/test_smoke.py` because the deletion needs to be staged. `git rm` in step 1.1 already staged it; including it in `git add` is redundant but harmless.)

---

## Task 2: `tier_mapping.py`

**Files:**
- Create: `src/door_sync/tier_mapping.py`
- Create: `tests/test_tier_mapping.py`

### Background

`tier_mapping.resolve(member, mapping)` produces a `ResolvedMember` from a `CiviMember` + `TierMapping`. Pure: imports `models` only.

Semantics (from spec §4.2):

1. Empty `membership_types` → `resolution="unmapped"`.
2. Any type missing from `mapping.rules` → `resolution="unmapped"`. (Fail-secure: one missing type unmaps the whole member.)
3. Otherwise, pick the rule with highest `rank`. On ties: sort by rank desc, then by type name asc, take first.

`resolve_all` is just a list comprehension over `resolve`.

- [ ] **Step 2.1: Write `tests/test_tier_mapping.py` — the full file**

```python
from door_sync.models import CiviMember, ResolvedMember, TierMapping, TierRule
from door_sync.tier_mapping import resolve, resolve_all


def _civi(types: list[str]) -> CiviMember:
    return CiviMember(
        contact_id=1, display_name="A", card_id=42, membership_types=types
    )


def test_single_tier_match() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(["Gold"]), mapping)
    assert result.resolution == "tier"
    assert result.target_policy == "P_GOLD"
    assert result.contact_id == 1
    assert result.display_name == "A"
    assert result.card_id == 42


def test_single_none_match() -> None:
    mapping = TierMapping(
        rules={"Comp": TierRule(resolution="none", target_policy=None, rank=1)}
    )
    result = resolve(_civi(["Comp"]), mapping)
    assert result.resolution == "none"
    assert result.target_policy is None


def test_single_day_pass_match() -> None:
    mapping = TierMapping(
        rules={"DayPass": TierRule(resolution="day-pass", target_policy=None, rank=1)}
    )
    result = resolve(_civi(["DayPass"]), mapping)
    assert result.resolution == "day-pass"
    assert result.target_policy is None


def test_unmapped_when_type_missing() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(["Silver"]), mapping)
    assert result.resolution == "unmapped"
    assert result.target_policy is None


def test_unmapped_when_membership_types_empty() -> None:
    mapping = TierMapping(rules={})
    result = resolve(_civi([]), mapping)
    assert result.resolution == "unmapped"
    assert result.target_policy is None


def test_highest_wins_two_tier_rules() -> None:
    mapping = TierMapping(
        rules={
            "Silver": TierRule(resolution="tier", target_policy="P_SILVER", rank=5),
            "Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10),
        }
    )
    result = resolve(_civi(["Silver", "Gold"]), mapping)
    assert result.target_policy == "P_GOLD"


def test_highest_wins_across_resolution_kinds() -> None:
    mapping = TierMapping(
        rules={
            "Comp": TierRule(resolution="none", target_policy=None, rank=99),
            "Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10),
        }
    )
    result = resolve(_civi(["Comp", "Gold"]), mapping)
    # Comp has higher rank, so its resolution wins (even though it's "none")
    assert result.resolution == "none"
    assert result.target_policy is None


def test_mixed_matched_and_unmatched_is_unmapped() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    result = resolve(_civi(["Gold", "MysteryType"]), mapping)
    # Fail-secure beats highest-wins
    assert result.resolution == "unmapped"


def test_tie_on_rank_resolves_deterministically() -> None:
    # Two rules at rank 5 — type name asc, so "A" beats "B"
    mapping = TierMapping(
        rules={
            "B_Type": TierRule(resolution="tier", target_policy="P_B", rank=5),
            "A_Type": TierRule(resolution="tier", target_policy="P_A", rank=5),
        }
    )
    result = resolve(_civi(["B_Type", "A_Type"]), mapping)
    assert result.target_policy == "P_A"


def test_resolve_all_preserves_order() -> None:
    mapping = TierMapping(
        rules={"Gold": TierRule(resolution="tier", target_policy="P_GOLD", rank=10)}
    )
    members = [
        CiviMember(contact_id=i, display_name=f"M{i}", card_id=i, membership_types=["Gold"])
        for i in (1, 2, 3)
    ]
    results = resolve_all(members, mapping)
    assert [r.contact_id for r in results] == [1, 2, 3]
    assert all(isinstance(r, ResolvedMember) for r in results)
```

The `pytest` import is unused — that's fine, ruff's default config doesn't flag unused imports in test files. If your ruff config does, remove the line.

- [ ] **Step 2.2: Run pytest to verify failure**

Run: `uv run pytest tests/test_tier_mapping.py -v`

Expected: collection error — `door_sync.tier_mapping` does not exist.

- [ ] **Step 2.3: Write `src/door_sync/tier_mapping.py` — the full file**

```python
from door_sync.models import CiviMember, ResolvedMember, TierMapping, TierRule


def resolve(member: CiviMember, mapping: TierMapping) -> ResolvedMember:
    if not member.membership_types:
        return ResolvedMember(
            contact_id=member.contact_id,
            display_name=member.display_name,
            card_id=member.card_id,
            target_policy=None,
            resolution="unmapped",
        )

    matched: list[tuple[str, TierRule]] = []
    for type_name in member.membership_types:
        rule = mapping.rules.get(type_name)
        if rule is None:
            return ResolvedMember(
                contact_id=member.contact_id,
                display_name=member.display_name,
                card_id=member.card_id,
                target_policy=None,
                resolution="unmapped",
            )
        matched.append((type_name, rule))

    # Highest-wins: sort by rank desc, then by type name asc; take the first.
    matched.sort(key=lambda t: (-t[1].rank, t[0]))
    chosen = matched[0][1]
    return ResolvedMember(
        contact_id=member.contact_id,
        display_name=member.display_name,
        card_id=member.card_id,
        target_policy=chosen.target_policy,
        resolution=chosen.resolution,
    )


def resolve_all(
    members: list[CiviMember], mapping: TierMapping
) -> list[ResolvedMember]:
    return [resolve(m, mapping) for m in members]
```

Note: we keep `TierRule` instances in the `matched` list rather than destructuring into a tuple of primitives. This preserves the `Literal["tier", "none", "day-pass"]` type on `chosen.resolution`, which is a subtype of `ResolvedMember.resolution`'s `Literal["tier", "none", "day-pass", "unmapped"]` — so mypy accepts the assignment without a type ignore.

- [ ] **Step 2.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: 10 new tests pass (plus the 11 from Task 1 = 21 total); mypy success; ruff clean.

- [ ] **Step 2.5: Commit**

```bash
git add src/door_sync/tier_mapping.py tests/test_tier_mapping.py
git commit -m "Add tier_mapping module with highest-wins resolution"
```

---

## Task 3: `reconciler.py`

**Files:**
- Create: `src/door_sync/reconciler.py`
- Create: `tests/test_reconciler.py`

### Background

`compute_diff(resolved, unifi) -> Diff` implements the architecture §8 truth table. Pure: imports `models` only.

Per spec §4.3, the implementation indexes both inputs by `contact_id`, iterates the union, and consults the truth table per contact. The trickiest row is "tier resolution, present + active": both `card_id`/`display_name` and `policy` can change independently, so a single contact can land in both `to_update_credential` and `to_update_policy`.

The idempotency canary uses a test-local `apply_diff_in_memory` helper that faithfully models what the eventual `UnifiClient.apply()` will do. If the diff algorithm and the in-memory projection agree, running `compute_diff` immediately after `apply_diff_in_memory` produces an all-empty `Diff`.

- [ ] **Step 3.1: Write `tests/test_reconciler.py` — the full file**

```python
from typing import Literal

from door_sync.models import Diff, ResolvedMember, UnifiUser
from door_sync.reconciler import compute_diff


def _resolved(
    contact_id: int = 1,
    display_name: str = "Alice",
    card_id: int | None = 100,
    target_policy: str | None = "P_GOLD",
    resolution: Literal["tier", "none", "day-pass", "unmapped"] = "tier",
) -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=display_name,
        card_id=card_id,
        target_policy=target_policy,
        resolution=resolution,
    )


def _unifi(
    contact_id: int = 1,
    display_name: str = "Alice",
    card_id: int | None = 100,
    active: bool = True,
    policy: str | None = "P_GOLD",
) -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=display_name,
        card_id=card_id,
        active=active,
        policy=policy,
    )


# --- Truth table rows from architecture §8 ---


def test_tier_not_in_unifi_adds() -> None:
    d = compute_diff([_resolved()], [])
    assert len(d.to_add) == 1
    assert d.to_add[0].contact_id == 1
    assert d.to_update_credential == []
    assert d.to_update_policy == []
    assert d.to_deactivate == []
    assert d.unmapped == []


def test_tier_card_id_differs_updates_credential() -> None:
    r = _resolved(card_id=200)
    u = _unifi(card_id=100)
    d = compute_diff([r], [u])
    assert d.to_update_credential == [(r, u)]
    assert d.to_update_policy == []
    assert d.to_add == []
    assert d.to_deactivate == []


def test_tier_display_name_differs_updates_credential() -> None:
    r = _resolved(display_name="Alice Renamed")
    u = _unifi(display_name="Alice")
    d = compute_diff([r], [u])
    assert d.to_update_credential == [(r, u)]
    assert d.to_update_policy == []


def test_tier_policy_differs_updates_policy() -> None:
    r = _resolved(target_policy="P_PLATINUM")
    u = _unifi(policy="P_GOLD")
    d = compute_diff([r], [u])
    assert d.to_update_policy == [(r, u)]
    assert d.to_update_credential == []


def test_tier_no_differences_is_noop() -> None:
    r = _resolved()
    u = _unifi()
    d = compute_diff([r], [u])
    assert d == Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )


def test_tier_present_inactive_re_adds() -> None:
    r = _resolved()
    u = _unifi(active=False)
    d = compute_diff([r], [u])
    assert d.to_add == [r]
    assert d.to_deactivate == []
    assert d.to_update_credential == []
    assert d.to_update_policy == []


def test_none_resolution_present_active_deactivates() -> None:
    r = _resolved(resolution="none", target_policy=None)
    u = _unifi()
    d = compute_diff([r], [u])
    assert d.to_deactivate == [u]
    assert d.to_add == []


def test_none_resolution_present_inactive_is_noop() -> None:
    r = _resolved(resolution="none", target_policy=None)
    u = _unifi(active=False)
    d = compute_diff([r], [u])
    assert d.to_deactivate == []
    assert d.to_add == []


def test_none_resolution_not_in_unifi_is_noop() -> None:
    r = _resolved(resolution="none", target_policy=None)
    d = compute_diff([r], [])
    assert d.to_deactivate == []
    assert d.to_add == []


def test_day_pass_resolution_is_always_noop() -> None:
    r = _resolved(resolution="day-pass", target_policy=None)
    # Present + active
    d1 = compute_diff([r], [_unifi()])
    # Present + inactive
    d2 = compute_diff([r], [_unifi(active=False)])
    # Not present
    d3 = compute_diff([r], [])
    for d in (d1, d2, d3):
        assert d == Diff(
            to_add=[],
            to_update_credential=[],
            to_update_policy=[],
            to_deactivate=[],
            unmapped=[],
        )


def test_unmapped_resolution_appended_to_unmapped() -> None:
    r = _resolved(resolution="unmapped", target_policy=None)
    d = compute_diff([r], [])
    assert d.unmapped == [r]
    assert d.to_add == []
    assert d.to_update_credential == []
    assert d.to_update_policy == []
    assert d.to_deactivate == []


def test_contact_only_in_unifi_active_deactivates() -> None:
    u = _unifi()
    d = compute_diff([], [u])
    assert d.to_deactivate == [u]


def test_contact_only_in_unifi_inactive_is_noop() -> None:
    u = _unifi(active=False)
    d = compute_diff([], [u])
    assert d.to_deactivate == []


# --- Combined-update test (architecture §8 last paragraph) ---


def test_tier_with_both_credential_and_policy_changes() -> None:
    r = _resolved(card_id=200, target_policy="P_PLATINUM")
    u = _unifi(card_id=100, policy="P_GOLD")
    d = compute_diff([r], [u])
    assert d.to_update_credential == [(r, u)]
    assert d.to_update_policy == [(r, u)]


# --- Idempotency canary (architecture §8) ---


def apply_diff_in_memory(diff: Diff, unifi: list[UnifiUser]) -> list[UnifiUser]:
    """Faithful in-memory model of the eventual UnifiClient.apply().

    Not production code — lives in the test file. If this drifts from the
    real apply(), the canary stops being meaningful; see spec §8 risks.
    """
    by_id: dict[int, UnifiUser] = {u.contact_id: u for u in unifi}

    for r in diff.to_add:
        by_id[r.contact_id] = UnifiUser(
            contact_id=r.contact_id,
            display_name=r.display_name,
            card_id=r.card_id,
            active=True,
            policy=r.target_policy,
        )

    for r, u in diff.to_update_credential:
        existing = by_id[u.contact_id]
        by_id[u.contact_id] = UnifiUser(
            contact_id=existing.contact_id,
            display_name=r.display_name,
            card_id=r.card_id,
            active=existing.active,
            policy=existing.policy,
        )

    for r, u in diff.to_update_policy:
        existing = by_id[u.contact_id]
        by_id[u.contact_id] = UnifiUser(
            contact_id=existing.contact_id,
            display_name=existing.display_name,
            card_id=existing.card_id,
            active=existing.active,
            policy=r.target_policy,
        )

    for u in diff.to_deactivate:
        existing = by_id[u.contact_id]
        by_id[u.contact_id] = UnifiUser(
            contact_id=existing.contact_id,
            display_name=existing.display_name,
            card_id=existing.card_id,
            active=False,
            policy=existing.policy,
        )

    return list(by_id.values())


def test_idempotency_canary() -> None:
    # A mix of every interesting state:
    #   - 1: tier, not in UniFi → to_add
    #   - 2: tier, card differs → to_update_credential
    #   - 3: tier, policy differs → to_update_policy
    #   - 4: tier, both differ → both updates
    #   - 5: none, present + active → to_deactivate
    #   - 6: not in resolved, present + active → to_deactivate
    #   - 7: tier, identical → no-op
    resolved = [
        _resolved(contact_id=1, card_id=10, target_policy="P_GOLD"),
        _resolved(contact_id=2, card_id=20, target_policy="P_GOLD"),
        _resolved(contact_id=3, card_id=30, target_policy="P_PLAT"),
        _resolved(contact_id=4, card_id=40, target_policy="P_PLAT"),
        _resolved(contact_id=5, resolution="none", target_policy=None, card_id=50),
        _resolved(contact_id=7, card_id=70, target_policy="P_GOLD"),
    ]
    unifi = [
        _unifi(contact_id=2, card_id=99, policy="P_GOLD"),
        _unifi(contact_id=3, card_id=30, policy="P_GOLD"),
        _unifi(contact_id=4, card_id=99, policy="P_GOLD"),
        _unifi(contact_id=5, card_id=50, policy="P_GOLD", active=True),
        _unifi(contact_id=6, card_id=60, policy="P_GOLD", active=True),
        _unifi(contact_id=7, card_id=70, policy="P_GOLD"),
    ]

    first = compute_diff(resolved, unifi)
    assert first.to_add  # sanity: this isn't a vacuous test
    assert first.to_deactivate

    new_unifi = apply_diff_in_memory(first, unifi)
    second = compute_diff(resolved, new_unifi)

    assert second == Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
```

- [ ] **Step 3.2: Run pytest to verify failure**

Run: `uv run pytest tests/test_reconciler.py -v`

Expected: collection error — `door_sync.reconciler` does not exist.

- [ ] **Step 3.3: Write `src/door_sync/reconciler.py` — the full file**

```python
from door_sync.models import Diff, ResolvedMember, UnifiUser


def compute_diff(
    resolved: list[ResolvedMember], unifi: list[UnifiUser]
) -> Diff:
    resolved_by_id: dict[int, ResolvedMember] = {r.contact_id: r for r in resolved}
    unifi_by_id: dict[int, UnifiUser] = {u.contact_id: u for u in unifi}

    to_add: list[ResolvedMember] = []
    to_update_credential: list[tuple[ResolvedMember, UnifiUser]] = []
    to_update_policy: list[tuple[ResolvedMember, UnifiUser]] = []
    to_deactivate: list[UnifiUser] = []
    unmapped: list[ResolvedMember] = []

    all_ids = set(resolved_by_id.keys()) | set(unifi_by_id.keys())

    for cid in all_ids:
        r = resolved_by_id.get(cid)
        u = unifi_by_id.get(cid)

        if r is None:
            # Contact only in UniFi: deactivate if currently active.
            assert u is not None  # all_ids construction guarantees this
            if u.active:
                to_deactivate.append(u)
            continue

        if r.resolution == "unmapped":
            unmapped.append(r)
            continue

        if r.resolution == "day-pass":
            # Never touch day-pass resolutions, regardless of UniFi state.
            continue

        if r.resolution == "none":
            if u is not None and u.active:
                to_deactivate.append(u)
            continue

        # r.resolution == "tier"
        if r.target_policy is None:
            # Malformed input — tier_mapping should never produce this.
            # Treat as no-op rather than raising (pure modules don't raise).
            continue

        if u is None or not u.active:
            to_add.append(r)
            continue

        # u present + active + tier resolution
        cred_changed = u.card_id != r.card_id or u.display_name != r.display_name
        pol_changed = u.policy != r.target_policy

        if cred_changed:
            to_update_credential.append((r, u))
        if pol_changed:
            to_update_policy.append((r, u))

    return Diff(
        to_add=to_add,
        to_update_credential=to_update_credential,
        to_update_policy=to_update_policy,
        to_deactivate=to_deactivate,
        unmapped=unmapped,
    )
```

- [ ] **Step 3.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: all reconciler tests pass (~15 new = 36 total); mypy success; ruff clean.

If the idempotency canary fails: the most likely cause is that `apply_diff_in_memory` and the diff algorithm disagree on what counts as "changed." Check that the field projection in `apply_diff_in_memory.to_update_credential` (display_name, card_id) matches the change-detection in `compute_diff` (display_name, card_id), and same for policy.

- [ ] **Step 3.5: Commit**

```bash
git add src/door_sync/reconciler.py tests/test_reconciler.py
git commit -m "Add reconciler with diff truth table and idempotency canary"
```

---

## Task 4: `safety.py`

**Files:**
- Create: `src/door_sync/safety.py`
- Create: `tests/test_safety.py`

### Background

`safety.check(diff, *, baseline, thresholds) -> CheckResult` runs the architecture §9 guards in this order (first to fire wins):

1. Unmapped types non-empty
2. Duplicate card IDs in `to_add` ∪ `to_update_credential` (resolved-member side of the tuple)
3. Invalid card ID (outside 0..65535) in the same collected set
4. Mass deactivation: `len(to_deactivate) / baseline > mass_deactivate_pct`
5. Mass addition: `len(to_add) / baseline > mass_add_pct`
6. Mass policy change: `len(to_update_policy) / baseline > mass_policy_pct`

Mass guards (4–6) are skipped when `baseline < thresholds.baseline_floor`. Integrity guards (1–3) always run.

The `reason` string for each halt is a stable, human-readable sentence. Tests assert it contains specific substrings rather than checking the exact format — that lets the wording be tweaked without breaking tests.

- [ ] **Step 4.1: Write `tests/test_safety.py` — the full file**

```python
from typing import Literal

from door_sync.models import (
    Diff,
    ResolvedMember,
    SafetyThresholds,
    UnifiUser,
)
from door_sync.safety import check


def _r(
    contact_id: int = 1,
    card_id: int | None = 100,
    target_policy: str | None = "P_GOLD",
    resolution: Literal["tier", "none", "day-pass", "unmapped"] = "tier",
) -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=f"M{contact_id}",
        card_id=card_id,
        target_policy=target_policy,
        resolution=resolution,
    )


def _u(contact_id: int = 1, active: bool = True) -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=f"U{contact_id}",
        card_id=100 + contact_id,
        active=active,
        policy="P_GOLD",
    )


def _empty_diff() -> Diff:
    return Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )


def _high_baseline() -> int:
    # Comfortably above default floor (10)
    return 100


def test_clean_diff_not_halted() -> None:
    result = check(_empty_diff(), baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is False
    assert result.reason is None


# --- Guard 1: unmapped types ---


def test_unmapped_non_empty_halts() -> None:
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[_r(resolution="unmapped", target_policy=None)],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert result.reason is not None
    assert "unmapped" in result.reason.lower()


# --- Guard 2: duplicate card IDs ---


def test_duplicate_card_in_to_add_halts() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=42), _r(contact_id=2, card_id=42)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert result.reason is not None
    assert "duplicate" in result.reason.lower()


def test_duplicate_card_across_add_and_update_credential_halts() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=42)],
        to_update_credential=[(_r(contact_id=2, card_id=42), _u(contact_id=2))],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert "duplicate" in (result.reason or "").lower()


def test_none_card_ids_dont_count_as_duplicates() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=None), _r(contact_id=2, card_id=None)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is False


# --- Guard 3: invalid card ID ---


def test_negative_card_id_halts() -> None:
    diff = Diff(
        to_add=[_r(card_id=-1)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert "invalid" in (result.reason or "").lower()


def test_card_id_above_65535_halts() -> None:
    diff = Diff(
        to_add=[_r(card_id=70000)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is True
    assert "invalid" in (result.reason or "").lower()


def test_card_id_at_boundary_0_and_65535_is_valid() -> None:
    diff = Diff(
        to_add=[_r(contact_id=1, card_id=0), _r(contact_id=2, card_id=65535)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=_high_baseline(), thresholds=SafetyThresholds())
    assert result.halted is False


# --- Guard 4: mass deactivation ---


def test_mass_deactivation_just_over_threshold_halts() -> None:
    # 16 / 100 = 16% > 15%
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_u(contact_id=i) for i in range(16)],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "deactivat" in (result.reason or "").lower()


def test_mass_deactivation_just_under_threshold_does_not_halt() -> None:
    # 15 / 100 = 15% not > 15%
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_u(contact_id=i) for i in range(15)],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is False


# --- Guard 5: mass addition ---


def test_mass_addition_over_threshold_halts() -> None:
    # 26 / 100 = 26% > 25%, with unique card ids to avoid tripping dup guard
    diff = Diff(
        to_add=[_r(contact_id=i, card_id=1000 + i) for i in range(26)],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "addition" in (result.reason or "").lower() or "add" in (result.reason or "").lower()


# --- Guard 6: mass policy change ---


def test_mass_policy_change_over_threshold_halts() -> None:
    # 21 / 100 = 21% > 20%
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[
            (_r(contact_id=i, card_id=2000 + i), _u(contact_id=i))
            for i in range(21)
        ],
        to_deactivate=[],
        unmapped=[],
    )
    result = check(diff, baseline=100, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "policy" in (result.reason or "").lower()


# --- Baseline floor behavior ---


def test_mass_guards_skipped_when_baseline_below_floor() -> None:
    # 5 to_deactivate, baseline=5 → would be 100%, way over 15%
    # But baseline=5 < floor=10, so guard skipped.
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[_u(contact_id=i) for i in range(5)],
        unmapped=[],
    )
    result = check(diff, baseline=5, thresholds=SafetyThresholds())
    assert result.halted is False


def test_integrity_guards_run_even_below_floor() -> None:
    # Below floor, but unmapped still trips.
    diff = Diff(
        to_add=[],
        to_update_credential=[],
        to_update_policy=[],
        to_deactivate=[],
        unmapped=[_r(resolution="unmapped", target_policy=None)],
    )
    result = check(diff, baseline=5, thresholds=SafetyThresholds())
    assert result.halted is True
    assert "unmapped" in (result.reason or "").lower()
```

- [ ] **Step 4.2: Run pytest to verify failure**

Run: `uv run pytest tests/test_safety.py -v`

Expected: collection error — `door_sync.safety` does not exist.

- [ ] **Step 4.3: Write `src/door_sync/safety.py` — the full file**

```python
from door_sync.models import CheckResult, Diff, SafetyThresholds


def check(
    diff: Diff,
    *,
    baseline: int,
    thresholds: SafetyThresholds,
) -> CheckResult:
    # Guard 1: unmapped types
    if diff.unmapped:
        return CheckResult(
            halted=True,
            reason=f"unmapped types: {len(diff.unmapped)} member(s) could not be mapped to a tier rule",
        )

    # Collect card_ids from diff entries that will write to UniFi.
    write_card_ids: list[int] = []
    for r in diff.to_add:
        if r.card_id is not None:
            write_card_ids.append(r.card_id)
    for r, _ in diff.to_update_credential:
        if r.card_id is not None:
            write_card_ids.append(r.card_id)

    # Guard 2: duplicate card IDs
    seen: set[int] = set()
    for cid in write_card_ids:
        if cid in seen:
            return CheckResult(
                halted=True,
                reason=f"duplicate card ID {cid} appears in multiple diff entries",
            )
        seen.add(cid)

    # Guard 3: invalid card ID
    for cid in write_card_ids:
        if cid < 0 or cid > 65535:
            return CheckResult(
                halted=True,
                reason=f"invalid card ID {cid}: must be in range 0..65535",
            )

    # Guards 4–6: mass guards, skipped when baseline is too small for percentages to be meaningful.
    if baseline >= thresholds.baseline_floor:
        # Guard 4: mass deactivation
        if len(diff.to_deactivate) / baseline > thresholds.mass_deactivate_pct:
            pct = len(diff.to_deactivate) / baseline * 100
            return CheckResult(
                halted=True,
                reason=(
                    f"mass deactivation: {len(diff.to_deactivate)} of {baseline} active users "
                    f"({pct:.1f}%) exceeds {thresholds.mass_deactivate_pct * 100:.1f}% threshold"
                ),
            )

        # Guard 5: mass addition
        if len(diff.to_add) / baseline > thresholds.mass_add_pct:
            pct = len(diff.to_add) / baseline * 100
            return CheckResult(
                halted=True,
                reason=(
                    f"mass addition: {len(diff.to_add)} new users vs baseline {baseline} "
                    f"({pct:.1f}%) exceeds {thresholds.mass_add_pct * 100:.1f}% threshold"
                ),
            )

        # Guard 6: mass policy change
        if len(diff.to_update_policy) / baseline > thresholds.mass_policy_pct:
            pct = len(diff.to_update_policy) / baseline * 100
            return CheckResult(
                halted=True,
                reason=(
                    f"mass policy change: {len(diff.to_update_policy)} of {baseline} active users "
                    f"({pct:.1f}%) exceeds {thresholds.mass_policy_pct * 100:.1f}% threshold"
                ),
            )

    return CheckResult(halted=False, reason=None)
```

- [ ] **Step 4.4: Run all three checks**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

Expected: all safety tests pass; total green count across all four files (~50 tests); mypy success; ruff clean.

- [ ] **Step 4.5: Commit**

```bash
git add src/door_sync/safety.py tests/test_safety.py
git commit -m "Add safety module with guards from architecture §9"
```

---

## Final verification

After Task 4 is complete and committed, do one more pass on a clean checkout to verify the slice meets every Definition-of-Done item from spec §2:

- [ ] **Step F.1: All three checks green from scratch**

```bash
uv run pytest -v
uv run mypy --strict src tests
uv run ruff check .
```

- [ ] **Step F.2: Verify the §8 truth table coverage**

```bash
uv run pytest tests/test_reconciler.py -v --collect-only
```

Count the test functions. There should be one named test per row of architecture §8 (rows 1–11), plus the combined-update test, plus the idempotency canary — about 15 tests.

- [ ] **Step F.3: Verify the §9 guard table coverage**

```bash
uv run pytest tests/test_safety.py -v --collect-only
```

There should be one named test per row of architecture §9 (6 guards) plus boundary/floor tests.

- [ ] **Step F.4: Verify pure modules have no I/O imports**

```bash
grep -nE "^(import|from)" src/door_sync/models.py src/door_sync/tier_mapping.py src/door_sync/reconciler.py src/door_sync/safety.py
```

Expected: only `from dataclasses ...`, `from typing ...`, and `from door_sync.models ...` lines. No `httpx`, no `logging`, no `os`, no `pathlib`.

- [ ] **Step F.5: Verify commit history**

```bash
git log --oneline -5
```

Expected: 4 module commits in order (models, tier_mapping, reconciler, safety) plus the earlier spec-and-architecture-revision commit.

If any of F.1–F.5 fails, fix the underlying issue and amend or add a new commit — do not mark the slice done.
