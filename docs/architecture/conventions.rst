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


Error Handling
--------------

The error strategy differs by layer:

**Pure modules** (``reconciler``, ``safety``, ``tier_mapping``) never raise
exceptions on data issues. They return sentinel values — for example,
``resolution="unmapped"`` or ``CheckResult(halted=True)`` — and let the
orchestrator decide how to handle them.

**Clients** (``civicrm.client``, ``unifi.client``) raise after exhausting
retries. Client exceptions propagate through the orchestrator to the scheduler.

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


Future: Webhook Receiver
------------------------

The architecture accommodates a future webhook receiver for the day-pass flow
(design guide Appendix C). When implemented:

- A new ``webhook.py`` module will contain a Flask application
- Flask runs via ``waitress`` in a second thread of the same daemon process
- Webhook handlers call into ``unifi.client`` visitor methods — they do **not**
  call ``orchestrator.reconcile()``
- The webhook uses a separate UniFi API key with Visitor scope only

The key constraint: the reconciler, safety, and tier_mapping modules remain
pure and untouched. The orchestrator's signature does not change. No async
migration is needed — the webhook is sync Flask.
