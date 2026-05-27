Usage
=====

Installation
------------

.. code-block:: bash

   uv sync

Commands
--------

Run the daemon loop (reconcile on a fixed cadence until SIGTERM/SIGINT):

.. code-block:: bash

   uv run door-sync run

Run a single reconciliation cycle and exit:

.. code-block:: bash

   uv run door-sync run --once

Dry-run mode (compute and log the diff without writing to UniFi):

.. code-block:: bash

   uv run door-sync run --once --dry-run

Print the computed diff without applying anything:

.. code-block:: bash

   uv run door-sync show-diff

Validate configuration and print any issues:

.. code-block:: bash

   uv run door-sync validate-config

Exit Codes
----------

=====  ===========
Code   Meaning
=====  ===========
0      Success (one-shot success; daemon clean shutdown)
1      Cycle halted by safety guards; config validation failed
2      Cycle crashed (``--once`` only — daemon catches and continues); ``show-diff`` fetch failed
64     CLI usage error
=====  ===========

Configuration
-------------

Configuration is split across two files:

- **TOML file** (``config.toml``): non-secret settings (hosts, thresholds, tier mapping rules)
- **Env file** (``.env``): secrets (API keys, SMTP credentials)

The config directory is resolved in order:

1. Explicit ``--config`` / ``--env-file`` CLI flags
2. ``$DOOR_SYNC_CONFIG_DIR`` environment variable
3. Current working directory (``./config.toml``, ``./.env``)

In production, files live at ``/etc/door-sync/config.toml`` and ``/etc/door-sync/env``
(mode 0400).


Deploying with systemd
----------------------

door-sync is designed to run as a long-lived systemd service on a Raspberry Pi
(or any Linux host). The steps below assume you are deploying to a dedicated
service account.

Creating a service account
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/door-sync door-sync

Installing the application
^^^^^^^^^^^^^^^^^^^^^^^^^^

Clone the repository and install dependencies under the service account's home
directory:

.. code-block:: bash

   sudo mkdir -p /opt/door-sync
   sudo chown door-sync:door-sync /opt/door-sync
   cd /opt/door-sync
   sudo -u door-sync git clone https://github.com/TechValleyCenterOfGravity/door-sync.git .
   sudo -u door-sync uv sync

Setting up configuration
^^^^^^^^^^^^^^^^^^^^^^^^

Create the configuration directory and files:

.. code-block:: bash

   sudo mkdir -p /etc/door-sync
   sudo cp config.toml.example /etc/door-sync/config.toml
   sudo cp .env.example /etc/door-sync/env

Lock down the secrets file:

.. code-block:: bash

   sudo chown door-sync:door-sync /etc/door-sync/env
   sudo chmod 0400 /etc/door-sync/env

Edit both files with your CiviCRM and UniFi Access connection details.

Set the ``DOOR_SYNC_CONFIG_DIR`` environment variable in the unit file (see
below) so the daemon finds the config directory automatically.

Creating operational directories
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The daemon writes audit logs, state, and alert flag files. Create the
directories and grant ownership:

.. code-block:: bash

   sudo mkdir -p /var/log/door-sync
   sudo mkdir -p /var/lib/door-sync
   sudo mkdir -p /var/run/door-sync
   sudo chown door-sync:door-sync /var/log/door-sync /var/lib/door-sync /var/run/door-sync

These paths are configurable in ``config.toml`` under ``[ops]``.

Installing the unit file
^^^^^^^^^^^^^^^^^^^^^^^^

A reference unit file is provided in the repository at
``deploy/door-sync.service``. Copy it into place:

.. code-block:: bash

   sudo cp /opt/door-sync/deploy/door-sync.service /etc/systemd/system/

The unit file contents:

.. literalinclude:: ../deploy/door-sync.service
   :language: ini

Enable and start the service:

.. code-block:: bash

   sudo systemctl daemon-reload
   sudo systemctl enable door-sync
   sudo systemctl start door-sync

Managing the service
^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   # Check status
   sudo systemctl status door-sync

   # Follow logs
   sudo journalctl -u door-sync -f

   # Restart after config changes
   sudo systemctl restart door-sync

   # Stop gracefully (finishes the current cycle, then exits)
   sudo systemctl stop door-sync

   # Run a one-off dry-run without affecting the daemon
   sudo -u door-sync DOOR_SYNC_CONFIG_DIR=/etc/door-sync \
       /opt/door-sync/.venv/bin/door-sync run --once --dry-run

Log rotation
^^^^^^^^^^^^

The audit log at ``/var/log/door-sync/audit.jsonl`` grows over time. It is
compatible with logrotate's ``copytruncate`` strategy (the daemon opens the
file in append mode per write, with no long-lived file handle).

A reference logrotate config is provided at ``deploy/door-sync.logrotate``.
Copy it into place:

.. code-block:: bash

   sudo cp /opt/door-sync/deploy/door-sync.logrotate /etc/logrotate.d/door-sync

The logrotate config contents:

.. literalinclude:: ../deploy/door-sync.logrotate
