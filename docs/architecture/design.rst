System Design
=============

Process Model
-------------

door-sync runs as a long-lived systemd service — not a cron job or
timer-triggered script. A single Python process starts at boot and runs until
it receives ``SIGTERM`` or ``SIGINT``.

Inside the process, a scheduler loop calls the orchestrator's ``reconcile()``
function, sleeps for ``cadence_seconds`` (configurable, default 600), and
repeats. The sleep uses ``threading.Event.wait(timeout=...)`` so a shutdown
signal can interrupt cleanly between cycles. In-flight cycles always finish;
no work is interrupted mid-cycle.

If a cycle crashes, the exception is caught by the scheduler, logged, and the
loop continues to the next cycle. A single failure does not bring down the
daemon.

Why Synchronous
^^^^^^^^^^^^^^^

There is no asyncio anywhere in the codebase. This is deliberate:

- The service does one burst of HTTP calls per cycle. There is no concurrency
  benefit from async I/O.
- The codebase is maintained by volunteer IT staff. Synchronous code is easier
  to read, debug, and extend than async/await patterns.
- HTTP is handled by ``httpx`` in synchronous mode.

Do not refactor to async without explicit direction from a human maintainer.


Module Layout
-------------

The codebase is organized into layers with strict dependency rules. Modules
higher in this list do not import modules lower in it.

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Module
     - Responsibility
     - Dependencies
   * - ``models``
     - All domain dataclasses (``CiviMember``, ``ResolvedMember``, ``UnifiUser``, ``Diff``, etc.)
     - stdlib only
   * - ``config``
     - Load and validate TOML + env into a frozen ``Config``
     - stdlib, ``models``
   * - ``tier_mapping``
     - Resolve a ``CiviMember`` into a ``ResolvedMember`` given the mapping rules
     - ``models`` (pure)
   * - ``reconciler``
     - Compute a ``Diff`` from resolved members and UniFi users
     - ``models`` (pure)
   * - ``safety``
     - Check a ``Diff`` against thresholds and integrity rules
     - ``models`` (pure)
   * - ``civicrm.client``
     - Read active members from CiviCRM API4
     - ``models``, ``httpx``
   * - ``unifi.client``
     - Read users from UniFi Access; apply a ``Diff``; honor ``dry_run`` flag
     - ``models``, ``httpx``
   * - ``audit``
     - Append-only JSONL log of every diff applied or halted
     - ``models``
   * - ``state``
     - Persist last-success/last-halt timestamps to a JSON file
     - stdlib only
   * - ``alert``
     - Dispatch alerts on halt or crash (flag-file + optional SMTP or Mailgun)
     - ``config``, ``httpx``, ``smtplib``
   * - ``orchestrator``
     - Wire everything together in a single ``reconcile()`` function
     - all of the above
   * - ``scheduler``
     - The daemon loop and signal handling
     - ``orchestrator``, ``config``

The orchestrator is the convergence point — it imports everything else. Nothing
imports the orchestrator except the scheduler (and, in the future, the webhook
receiver).


The Pure/Impure Boundary
------------------------

Three modules are designated **pure**: ``reconciler``, ``safety``, and
``tier_mapping``. This means:

- They take frozen dataclasses as input and return frozen dataclasses as output
- They perform no I/O — no HTTP, no file access, no logging
- They have no global state and do not mutate their arguments
- They are deterministic given the same inputs

This matters for three reasons:

**Testing is trivial.** Pure-module tests construct dataclasses directly and
assert on the output. No mocks, no HTTP fixtures, no setup/teardown. Tests run
in milliseconds.

**Dry-run is trustworthy.** The ``dry_run`` flag lives inside ``UnifiClient``
and turns writes into logged no-ops. Because the pure modules execute
identically regardless of dry-run, a clean dry-run on production data means the
next live run will compute the same diff.

**Correctness is concentrated.** These three modules carry the entire
correctness story of the service. The API clients are thin HTTP wrappers; the
orchestrator is wiring. If you need to understand *what* the service does to
your data, read the pure modules.

Rules for keeping them pure:

- No ``logging`` calls. The orchestrator logs inputs and outputs.
- No config lookups. Pass the relevant values as arguments.
- No exceptions on data issues. Return sentinel values (e.g.,
  ``resolution="unmapped"``, ``CheckResult(halted=True)``) and let the
  orchestrator decide how to handle them.
