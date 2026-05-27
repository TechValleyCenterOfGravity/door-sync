Configuration
=============

door-sync uses a two-file configuration split: non-secret settings live in a
TOML file, and secrets live in a ``KEY=value`` env file. This separation allows
the TOML file to be version-controlled while keeping credentials out of the
repository.


File Discovery
--------------

The daemon locates its configuration files in this order:

1. Explicit CLI flags: ``--config <path>`` and ``--env-file <path>``
2. The ``DOOR_SYNC_CONFIG_DIR`` environment variable — if set, the daemon looks
   for ``config.toml`` and ``env`` inside that directory
3. The current working directory: ``./config.toml`` and ``./.env``

In production the systemd unit file sets
``Environment=DOOR_SYNC_CONFIG_DIR=/etc/door-sync``, so the daemon reads
``/etc/door-sync/config.toml`` and ``/etc/door-sync/env``.

You can validate your configuration without running a sync cycle:

.. code-block:: bash

   uv run door-sync validate-config


Env File (Secrets)
------------------

The env file uses a simple ``KEY=value`` format compatible with systemd's
``EnvironmentFile`` directive. Blank lines and ``#`` comments are allowed.
Values may be optionally quoted with single or double quotes.

When a key appears in both the env file and the process environment, the
**env file wins**. The process environment is only consulted for keys not
present in the file.

Required Variables
^^^^^^^^^^^^^^^^^^

These variables are always required:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Description
   * - ``CIVICRM_API_KEY``
     - Bearer token for the CiviCRM API4 endpoint.
   * - ``UNIFI_API_KEY``
     - Bearer token for the UniFi Access local API.

Conditional Variables
^^^^^^^^^^^^^^^^^^^^^

These are required only when the corresponding alert transport is enabled:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Required when
   * - ``SMTP_USERNAME``
     - ``alert.transport = "smtp"``
   * - ``SMTP_PASSWORD``
     - ``alert.transport = "smtp"``
   * - ``MAILGUN_API_KEY``
     - ``alert.transport = "mailgun"``

Example env file:

.. code-block:: bash

   # CiviCRM
   CIVICRM_API_KEY=your-civicrm-api-key-here

   # UniFi Access
   UNIFI_API_KEY=your-unifi-api-key-here

   # Only needed if alert.transport = "smtp"
   # SMTP_USERNAME=alerts@example.org
   # SMTP_PASSWORD=smtp-password-here

   # Only needed if alert.transport = "mailgun"
   # MAILGUN_API_KEY=key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx


TOML File Reference
-------------------

Below is a complete annotated example of ``config.toml`` showing every
available setting with its default value.


Top-level
^^^^^^^^^

.. code-block:: toml

   # Seconds between reconciliation cycles in daemon mode.
   # Minimum: 60. Default: 600 (10 minutes).
   cadence_seconds = 600


``[civicrm]`` — CiviCRM Connection
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: toml

   [civicrm]
   # Base URL of the WordPress site hosting CiviCRM. Must use https.
   host = "https://crm.example.org"

   # The CiviCRM custom field API name that stores the door card ID.
   # This is the field name as it appears in API4 (e.g., "custom_42"
   # or a custom field group name like "Contact_Card.Card_Number").
   card_id_field = "Contact_Card.Card_Number"

The API key is read from the ``CIVICRM_API_KEY`` env variable (see above).


``[unifi]`` — UniFi Access Connection
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: toml

   [unifi]
   # Base URL of the UniFi Access controller. Must use https.
   # The local API listens on port 12445 by default; if your URL
   # omits the port, 12445 is assumed.
   host = "https://unifi-access.local:12445"

   # SHA-256 fingerprint of the controller's TLS certificate.
   # Used for certificate pinning (the controller uses a self-signed cert).
   # Accepted formats:
   #   - 64 hex characters: "aabbcc...ff"
   #   - 32 colon-separated byte pairs: "AA:BB:CC:...:FF"
   # To obtain the fingerprint:
   #   openssl s_client -connect <host>:12445 < /dev/null 2>/dev/null \
   #       | openssl x509 -noout -fingerprint -sha256
   tls_fingerprint = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"

   # Wiegand-26 facility code (0-255). Must match the facility code
   # programmed into your NFC card stock. Used to encode and decode
   # the nfc_id values that the UniFi API expects.
   facility_code = 42

The API key is read from the ``UNIFI_API_KEY`` env variable.


``[safety]`` — Safety Guard Thresholds
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

These thresholds control the safety guards that prevent the daemon from making
unexpectedly large changes in a single cycle. Each percentage threshold is
expressed as a fraction (0.0 to 1.0) of the active UniFi user baseline.

.. code-block:: toml

   [safety]
   # Halt if more than this fraction of active users would be deactivated.
   # Default: 0.15 (15%).
   mass_deactivate_pct = 0.15

   # Halt if new additions exceed this fraction of the active baseline.
   # Default: 0.25 (25%).
   mass_add_pct = 0.25

   # Halt if policy changes exceed this fraction of active users.
   # Default: 0.20 (20%).
   mass_policy_pct = 0.20

   # Minimum number of active UniFi users before percentage-based guards
   # engage. Below this floor, only the absolute guards (unmapped types,
   # duplicate/invalid card IDs) are checked. This prevents the percentage
   # guards from blocking legitimate initial provisioning on a small
   # population.
   # Default: 10.
   baseline_floor = 10


``[tier_mapping]`` — Membership-to-Policy Rules
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The tier mapping defines how CiviCRM membership types translate into UniFi
access policies. Each rule is a TOML sub-table under
``[tier_mapping.rules]``, keyed by the **exact** CiviCRM membership type
label (case-sensitive).

.. code-block:: toml

   [tier_mapping.rules.General]
   resolution = "tier"
   target_policy = "64f1a2b3c4d5e6f7a8b9c0d1"   # UniFi access policy ID
   rank = 10

   [tier_mapping.rules.Premium]
   resolution = "tier"
   target_policy = "74a2b3c4d5e6f7a8b9c0d1e2"
   rank = 20

   [tier_mapping.rules."Day Pass"]
   resolution = "day-pass"
   rank = 5

   [tier_mapping.rules.Suspended]
   resolution = "none"
   rank = 0

Each rule has three fields:

``resolution``
    How to handle members with this membership type. One of:

    - ``"tier"`` — provision the member in UniFi with the specified access
      policy. Requires ``target_policy``.
    - ``"none"`` — the member should not have door access. Any existing UniFi
      user for this contact will be deactivated.
    - ``"day-pass"`` — the member is managed by a separate day-pass system.
      The reconciler ignores them entirely.

``target_policy``
    The UniFi Access policy ID to assign. **Required** when
    ``resolution = "tier"``, **must be omitted** otherwise. You can find
    policy IDs in the UniFi Access web UI or via the API.

``rank``
    Integer priority. When a contact holds multiple active memberships, the
    rule with the highest rank wins. Ties are broken alphabetically by
    membership type name.

.. important::

   Every active CiviCRM membership type must have a corresponding rule. If a
   member has a type with no matching rule, the safety guard will halt the
   cycle with an "unmapped types" error.


``[ops]`` — Operational File Paths
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

These paths control where the daemon writes its audit log, persistent state,
and alert flag file. All three directories must exist and be writable by the
service account.

.. code-block:: toml

   [ops]
   # Append-only JSONL audit log. One record per cycle outcome.
   # Default: /var/log/door-sync/audit.jsonl
   audit_jsonl = "/var/log/door-sync/audit.jsonl"

   # Persistent state (last success/halt timestamps, run counter).
   # Default: /var/lib/door-sync/state.json
   state_json = "/var/lib/door-sync/state.json"

   # Alert flag file. Presence indicates an active alert condition.
   # External monitoring (Nagios, Prometheus) can check for this file.
   # Default: /var/run/door-sync/alert.flag
   alert_flag = "/var/run/door-sync/alert.flag"

For development, you may want to override these to local paths:

.. code-block:: toml

   [ops]
   audit_jsonl = "./data/audit.jsonl"
   state_json = "./data/state.json"
   alert_flag = "./data/alert.flag"


``[alert]`` — Alert Transport
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

When a safety guard halts a cycle or the daemon crashes, an alert is
dispatched. The flag file (``ops.alert_flag``) is **always** written
regardless of the transport setting — it serves as a simple signal for
external monitoring tools.

Optionally, an email alert can be sent via SMTP or Mailgun.

Flag-file only (default)
""""""""""""""""""""""""

.. code-block:: toml

   [alert]
   transport = "flag-file"

No email is sent. The flag file is written on alert and removed on the next
successful cycle.

SMTP transport
""""""""""""""

.. code-block:: toml

   [alert]
   transport = "smtp"

   [alert.smtp]
   host = "smtp.example.org"
   port = 587                              # Default: 587
   starttls = true                         # Default: true. Set false for SMTP_SSL (port 465).
   from = "door-sync@example.org"
   to = ["ops-team@example.org"]           # Single address or list of addresses
   subject_prefix = "[door-sync]"          # Default: "[door-sync]"

SMTP credentials are read from the ``SMTP_USERNAME`` and ``SMTP_PASSWORD``
env variables.

Mailgun transport
"""""""""""""""""

.. code-block:: toml

   [alert]
   transport = "mailgun"

   [alert.mailgun]
   domain = "mg.example.org"
   from = "door-sync@mg.example.org"
   to = ["ops-team@example.org"]
   subject_prefix = "[door-sync]"          # Default: "[door-sync]"

The Mailgun API key is read from the ``MAILGUN_API_KEY`` env variable.

.. note::

   Email delivery failures are logged at ERROR but never crash a
   reconciliation cycle. The flag file is the reliable alert mechanism;
   email is a convenience layer.


Complete Example
----------------

Putting it all together, here is a minimal production ``config.toml``:

.. code-block:: toml

   cadence_seconds = 600

   [civicrm]
   host = "https://crm.example.org"
   card_id_field = "Contact_Card.Card_Number"

   [unifi]
   host = "https://unifi-access.local:12445"
   tls_fingerprint = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"
   facility_code = 42

   [safety]
   mass_deactivate_pct = 0.15
   mass_add_pct = 0.25
   mass_policy_pct = 0.20
   baseline_floor = 10

   [tier_mapping.rules.General]
   resolution = "tier"
   target_policy = "64f1a2b3c4d5e6f7a8b9c0d1"
   rank = 10

   [tier_mapping.rules.Premium]
   resolution = "tier"
   target_policy = "74a2b3c4d5e6f7a8b9c0d1e2"
   rank = 20

   [tier_mapping.rules."Day Pass"]
   resolution = "day-pass"
   rank = 5

   [ops]
   audit_jsonl = "/var/log/door-sync/audit.jsonl"
   state_json = "/var/lib/door-sync/state.json"
   alert_flag = "/var/run/door-sync/alert.flag"

   [alert]
   transport = "flag-file"

And the corresponding ``env`` file:

.. code-block:: bash

   CIVICRM_API_KEY=your-civicrm-api-key
   UNIFI_API_KEY=your-unifi-api-key
