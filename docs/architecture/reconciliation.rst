Reconciliation
==============

This section explains the data flow through a single reconciliation cycle:
how members are resolved, how the diff is computed, and what the safety guards
protect against.


Data Contracts
--------------

All data flows through frozen dataclasses defined in ``models.py``. The key
types are:

:class:`~door_sync.models.CiviMember`
    A contact fetched from CiviCRM with a card ID and their active membership
    type labels.

:class:`~door_sync.models.ResolvedMember`
    A ``CiviMember`` after tier-mapping resolution. Carries a ``resolution``
    field (``tier``, ``none``, ``day-pass``, or ``unmapped``) and a
    ``target_policy`` for the ``tier`` case.

:class:`~door_sync.models.UnifiUser`
    A user record from the UniFi Access controller. The ``contact_id`` field
    is stored in UniFi's ``employee_number`` field — this is the reconciliation
    key that links the two systems.

:class:`~door_sync.models.Diff`
    The computed difference: who to add, whose credentials changed, whose
    policy changed, who to deactivate, and who couldn't be mapped.

All dataclasses are ``frozen=True``. Never mutate them; construct new instances
instead.


Tier Mapping
------------

Before comparing against UniFi, each CiviCRM member's membership types are
resolved to a target access policy. The ``tier_mapping.resolve()`` function
takes a member and the configured mapping rules, and returns one of four
resolution outcomes:

**tier**
    The member maps to a specific access policy. They should have an active
    UniFi user with the specified credentials and policy.

**none**
    The member's membership type explicitly means "no door access." Any
    existing UniFi user for this contact should be deactivated.

**day-pass**
    The member is handled by the day-pass flow (a separate system). The
    reconciler skips them entirely — it will not provision, deactivate, or
    modify any existing UniFi record for this contact.

**unmapped**
    The member has a membership type with no matching rule in the configuration.
    This is treated as a data issue: the safety guard will halt the cycle so an
    operator can add the missing rule.

When a contact holds multiple active memberships at different tiers, the
highest-ranked rule wins (sorted by the ``rank`` field in the config, then
alphabetically by type name as a tiebreaker).


The Diff Algorithm
------------------

``reconciler.compute_diff()`` indexes both the resolved members and UniFi users
by ``contact_id``, then walks the union of all IDs to classify each contact:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - CiviCRM (resolved)
     - UniFi
     - Action
   * - ``tier`` resolution
     - not present, or present but inactive
     - **to_add** (create or reactivate)
   * - ``tier`` resolution
     - active, card or name differs
     - **to_update_credential**
   * - ``tier`` resolution
     - active, policy differs
     - **to_update_policy**
   * - ``tier`` resolution
     - active, no differences
     - no-op
   * - ``none`` resolution
     - active
     - **to_deactivate**
   * - ``day-pass`` resolution
     - any
     - no-op (explicitly skipped)
   * - ``unmapped`` resolution
     - any
     - **unmapped** (triggers safety halt)
   * - not in CiviCRM
     - active
     - **to_deactivate**

A contact can appear in both ``to_update_credential`` and ``to_update_policy``
in the same diff if both changed. Both updates are applied.

**Idempotency invariant:** running ``compute_diff`` immediately after a
successful ``apply()`` must produce a diff with all empty sets. This is the
canonical correctness test for the algorithm.


Safety Guards
-------------

Before any diff is applied, ``safety.check()`` evaluates a series of guards.
If **any** guard fires, the entire cycle is halted — no partial application.

.. list-table::
   :header-rows: 1
   :widths: 25 50 25

   * - Guard
     - Trigger
     - Default
   * - Unmapped types
     - Any member could not be mapped to a tier rule
     - any
   * - Duplicate card IDs
     - Two members share the same non-None ``card_id``
     - any
   * - Invalid card ID
     - A ``card_id`` is outside the Wiegand-26 range (0--65535)
     - any
   * - Mass deactivation
     - Deactivations exceed a percentage of active users
     - 15%
   * - Mass addition
     - Additions exceed a percentage of the active baseline
     - 25%
   * - Mass policy change
     - Policy changes exceed a percentage of active users
     - 20%

The percentage-based guards use the count of *active* UniFi users as the
baseline. When the baseline is below a configurable floor (default 10), the
percentage guards are skipped — they would be meaningless on tiny populations
and would block legitimate initial provisioning.

All thresholds are configurable in ``config.toml`` under ``[safety]``.

When a guard fires:

1. The diff is written to the audit log as a ``halted`` event
2. An alert is dispatched (flag file + optional email)
3. The halt reason is recorded in the state file
4. Zero writes are made to UniFi


The Orchestrator
----------------

``orchestrator.reconcile()`` is the single entry point for a reconciliation
cycle. It wires together all the pieces described above:

1. Construct a ``CivicrmClient`` and ``UnifiClient`` (as context managers)
2. Fetch members and users
3. Resolve tier mappings
4. Compute the diff
5. Run safety guards
6. Apply the diff (or halt)
7. Write audit records and update state

Key design properties:

- **No globals.** Everything comes from the ``Config`` object.
- **Clients are per-cycle.** They're cheap to construct and this gives clean
  isolation between cycles, avoiding stale HTTP sessions.
- **Exceptions propagate.** The orchestrator does not catch — the scheduler's
  per-cycle ``try/except`` handles crashes. Within ``apply()`` itself, a single
  contact's failure is isolated (logged and skipped) so the rest of the cycle
  still applies; a summary error is then raised so the failure still surfaces.
  Email writes are best-effort: an address already registered to another UniFi
  account is dropped and warned rather than failing the contact.
- **One function, many callers.** The same ``reconcile()`` is called by the
  daemon loop, the ``--once`` CLI mode, and (in the future) the webhook handler.
