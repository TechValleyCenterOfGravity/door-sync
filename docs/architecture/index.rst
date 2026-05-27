Architecture
============

door-sync is a reconciliation daemon that keeps UniFi Access door credentials
in sync with CiviCRM membership data. It runs on a Raspberry Pi under systemd,
polling on a fixed cadence (default: every 10 minutes).

The most important thing to understand about the architecture is that
**door-sync is not in the critical path for door authorization.** Doors
authorize locally against credentials cached on the UniFi Retrofit Hub. The
sync service is an eventually-consistent reconciler — if it goes down, doors
keep working with their last-known state. This means the service prioritizes
correctness and safety over speed.

Each reconciliation cycle follows a straightforward pipeline:

1. Fetch active members from CiviCRM
2. Resolve each member's membership types to an access policy via tier-mapping rules
3. Fetch the current user list from UniFi Access
4. Compute the diff between the two
5. Run safety guards against the diff
6. Apply the diff (or halt if a guard fires)

The sections below explain the design decisions behind this pipeline.

.. toctree::
   :maxdepth: 2

   design
   reconciliation
   conventions
