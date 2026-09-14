Conventions
===========

This section covers the coding conventions and operational patterns used
throughout the codebase.


Type Hints
----------

Type hints are used everywhere, including private functions. The project uses
``pyrefly`` as its type checker (run via ``uv run pyrefly check``).


Dataclasses
-----------

All domain objects use ``@dataclass(frozen=True)``. Mutable containers
(``list``, ``dict``) inside frozen dataclasses are treated as conceptually
immutable — never mutate them in place. Construct a new dataclass instance with
the updated value instead.


Dependency Injection
--------------------

Dependencies are passed as arguments. There are no module-level singletons for
clients, configuration, or loggers (aside from the stdlib ``logging`` tree).


HTTP
----

All HTTP is synchronous ``httpx``. Each client class owns one ``httpx.Client``
instance and must be used as a context manager (or have ``close()`` called
explicitly) to avoid connection leaks.

The UniFi controller uses a self-signed TLS certificate. Rather than disabling
verification, the client pins the certificate's SHA-256 fingerprint via
configuration and validates it on each connection.


Logging
-------

The service uses two separate logging streams:

**Operational logging** goes to stderr (inherited by the systemd journal) via
the stdlib ``logging`` module. Log levels:

- **DEBUG** — verbose detail, enabled with ``-v``
- **INFO** — normal cycle output
- **WARNING** — retryable failures
- **ERROR** — halts and crashes

**Audit logging** goes to a dedicated JSONL file at
``/var/log/door-sync/audit.jsonl`` (configurable). Every diff applied or halted
produces a structured JSON record. This stream is for incident review and
reporting, not for debugging.


Card ID Redaction
-----------------

Card IDs are security-sensitive. They appear in audit logs and operational logs
as last-4-digits only (e.g., ``****1234``). Full card IDs are never logged at
any level.

Member names are likewise kept out of the operational and audit log streams.
Log records and alerts identify a member by CiviCRM ``contact_id`` (and, for an
unmanaged UniFi account, its user id) — never by name — so member PII does not
accumulate in the journal. A ``contact_id`` resolves back to the member in
CiviCRM when an operator needs the name. (The interactive ``show-diff`` CLI does
print names to the operator's terminal on demand; that is direct operator
output, not a persisted log.)


Error Handling
--------------

The error strategy differs by layer:

**Pure modules** (``reconciler``, ``safety``, ``tier_mapping``) never raise
exceptions on data issues. They return sentinel values — for example,
``resolution="unmapped"`` or ``CheckResult(halted=True)`` — and let the
orchestrator decide how to handle them.

**Clients** (``civicrm.client``, ``unifi.client``) raise after exhausting
retries. Client exceptions propagate through the orchestrator to the scheduler,
with two refinements inside ``unifi.apply()``:

- **Per-user isolation.** A single contact's ``UnifiClientError`` is logged,
  recorded, and skipped so the remaining contacts still apply; ``apply()`` then
  raises one summary error at the end so the failure is still surfaced.
- **Best-effort email.** UniFi requires globally-unique emails across users and
  admins, so an email already registered to another account is dropped from the
  write (the rest of the record still applies) and warned — not treated as a
  failure.

**The scheduler** catches per-cycle exceptions, logs them, writes a crash
audit record, and continues to the next cycle.


Testing
-------

Tests use ``pytest`` and live in the ``tests/`` directory. The test strategy
follows the pure/impure boundary:

- **Pure-module tests** use plain dataclass construction. No mocks, no HTTP
  fixtures. These tests are fast and thorough.
- **Client tests** use ``pytest-httpx`` to mock HTTP responses.
- **Orchestrator tests** fake both clients to verify the wiring.


Webhook Receiver
----------------

``webhook.py`` is a sync Flask application served by ``waitress`` in a second
thread of the daemon. It is optional and disabled by default. See
``architecture.md`` §13 for the full design; the conventions that matter when
touching it:

- The HTTP thread **writes nothing**. It verifies the HMAC, enqueues a
  ``ReconcileRequest`` and returns 202. The scheduler drains the queue and stays
  the sole writer to UniFi, state, and the audit log, so no locks are needed.
- ``webhook`` does **not** import ``orchestrator``. A trigger reaches
  ``reconcile()`` only by way of the scheduler.
- The bind address must be loopback; config validation enforces it rather than
  merely documenting it.
- Signal handlers set an ``Event`` and nothing else — never a queue operation,
  which can self-deadlock on ``queue.Queue``'s non-reentrant mutex.
- Logs identify members by ``contact_id`` only, never by name or card ID.

Still future: the day-pass flow (design guide Appendix C) adds
``/day-pass/provision`` and ``/day-pass/revoke`` to the same receiver. Those
handlers call ``unifi.client`` visitor methods directly with a separate
Visitor-scope API key — they do not call ``orchestrator.reconcile()`` and do not
enqueue reconcile triggers.

The key constraint is unchanged: the reconciler, safety, and tier_mapping
modules remain pure and untouched, the orchestrator's signature does not change,
and there is no async migration — the receiver is sync Flask.
