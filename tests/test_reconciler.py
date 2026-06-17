from typing import Literal

from door_sync.models import Diff, ResolvedMember, UnifiUser
from door_sync.reconciler import compute_diff


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


# --- Truth table rows from architecture §8 ---


def test_tier_not_in_unifi_adds() -> None:
    d = compute_diff([_resolved()], [])
    assert len(d.to_add) == 1
    assert d.to_add[0].contact_id == 1
    assert d.to_update_credential == ()
    assert d.to_update_policy == ()
    assert d.to_deactivate == ()
    assert d.unmapped == ()


def test_tier_card_id_differs_updates_credential() -> None:
    r = _resolved(card_id=200)
    u = _unifi(card_id=100)
    d = compute_diff([r], [u])
    assert d.to_update_credential == ((r, u),)
    assert d.to_update_policy == ()
    assert d.to_add == ()
    assert d.to_deactivate == ()


def test_tier_display_name_differs_updates_credential() -> None:
    r = _resolved(display_name="Alice Renamed")
    u = _unifi(display_name="Alice")
    d = compute_diff([r], [u])
    assert d.to_update_credential == ((r, u),)
    assert d.to_update_policy == ()


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


def test_tier_policy_differs_updates_policy() -> None:
    r = _resolved(target_policy="P_PLATINUM")
    u = _unifi(policy="P_GOLD")
    d = compute_diff([r], [u])
    assert d.to_update_policy == ((r, u),)
    assert d.to_update_credential == ()


def test_tier_no_differences_is_noop() -> None:
    r = _resolved()
    u = _unifi()
    d = compute_diff([r], [u])
    assert d == Diff(
        to_add=(),
        to_update_credential=(),
        to_update_policy=(),
        to_deactivate=(),
        unmapped=(),
    )


def test_tier_present_inactive_re_adds() -> None:
    r = _resolved()
    u = _unifi(active=False)
    d = compute_diff([r], [u])
    assert d.to_add == (r,)
    assert d.to_deactivate == ()
    assert d.to_update_credential == ()
    assert d.to_update_policy == ()


def test_none_resolution_present_active_deactivates() -> None:
    r = _resolved(resolution="none", target_policy=None)
    u = _unifi()
    d = compute_diff([r], [u])
    assert d.to_deactivate == (u,)
    assert d.to_add == ()


def test_none_resolution_present_inactive_is_noop() -> None:
    r = _resolved(resolution="none", target_policy=None)
    u = _unifi(active=False)
    d = compute_diff([r], [u])
    assert d.to_deactivate == ()
    assert d.to_add == ()


def test_none_resolution_not_in_unifi_is_noop() -> None:
    r = _resolved(resolution="none", target_policy=None)
    d = compute_diff([r], [])
    assert d.to_deactivate == ()
    assert d.to_add == ()


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
            to_add=(),
            to_update_credential=(),
            to_update_policy=(),
            to_deactivate=(),
            unmapped=(),
        )


def test_unmapped_resolution_appended_to_unmapped() -> None:
    r = _resolved(resolution="unmapped", target_policy=None)
    d = compute_diff([r], [])
    assert d.unmapped == (r,)
    assert d.to_add == ()
    assert d.to_update_credential == ()
    assert d.to_update_policy == ()
    assert d.to_deactivate == ()


def test_contact_only_in_unifi_active_deactivates() -> None:
    u = _unifi()
    d = compute_diff([], [u])
    assert d.to_deactivate == (u,)


def test_contact_only_in_unifi_inactive_is_noop() -> None:
    u = _unifi(active=False)
    d = compute_diff([], [u])
    assert d.to_deactivate == ()


# --- Combined-update test (architecture §8 last paragraph) ---


def test_tier_with_both_credential_and_policy_changes() -> None:
    r = _resolved(card_id=200, target_policy="P_PLATINUM")
    u = _unifi(card_id=100, policy="P_GOLD")
    d = compute_diff([r], [u])
    assert d.to_update_credential == ((r, u),)
    assert d.to_update_policy == ((r, u),)


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
            email=r.email,
        )

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

    for r, u in diff.to_update_policy:
        existing = by_id[u.contact_id]
        by_id[u.contact_id] = UnifiUser(
            contact_id=existing.contact_id,
            display_name=existing.display_name,
            card_id=existing.card_id,
            active=existing.active,
            policy=r.target_policy,
            email=existing.email,
        )

    for u in diff.to_deactivate:
        existing = by_id[u.contact_id]
        by_id[u.contact_id] = UnifiUser(
            contact_id=existing.contact_id,
            display_name=existing.display_name,
            card_id=existing.card_id,
            active=False,
            policy=existing.policy,
            email=existing.email,
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
    # Sanity: every diff bucket the canary is supposed to exercise must be non-empty.
    # If a future edit to the fixture drops one, the canary would silently stop testing that path.
    assert first.to_add
    assert first.to_update_credential
    assert first.to_update_policy
    assert first.to_deactivate

    new_unifi = apply_diff_in_memory(first, unifi)
    second = compute_diff(resolved, new_unifi)

    assert second == Diff(
        to_add=(),
        to_update_credential=(),
        to_update_policy=(),
        to_deactivate=(),
        unmapped=(),
    )
