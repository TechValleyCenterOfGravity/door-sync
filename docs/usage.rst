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

Configuration is split across two files: a TOML file for non-secret settings
and an env file for secrets (API keys, SMTP credentials). See the
:doc:`configuration` page for a complete reference of every setting.


Deploying with systemd
----------------------

door-sync runs as a long-lived systemd service on a Raspberry Pi (or any Linux
host). The current deployment is a **Raspberry Pi 3 running Raspberry Pi OS
Lite (64-bit)**, installed from the Debian package.

.. note::

   The immutable A/B appliance image under ``deploy/rpi-image-gen/`` is a
   different deployment model and requires a **Raspberry Pi 4 or later** —
   Raspberry Pi's A/B boot updates do not support the Pi 3. This page is the
   path in use today.

Installing the package
^^^^^^^^^^^^^^^^^^^^^^

Install from a release asset. The package is ``Architecture: all``, so one
``.deb`` serves any Pi:

.. code-block:: bash

   VERSION=0.3.0
   curl -fsSLO "https://github.com/TechValleyCenterOfGravity/door-sync/releases/download/v${VERSION}/door-sync_${VERSION}_all.deb"
   sudo apt install "./door-sync_${VERSION}_all.deb"

``apt`` resolves the runtime dependencies (``python3-flask``, ``python3-httpx``,
``python3-waitress``) from Debian, so nothing is vendored into a virtualenv and
everything door-sync runs is recorded by dpkg.

The package does the setup this page used to describe by hand: it creates the
``door-sync`` service account, ``/etc/door-sync``, ``/var/lib/door-sync`` and
``/var/log/door-sync``, installs ``door-sync.service`` and the logrotate
config, and places examples under ``/usr/share/door-sync/``. It does **not**
start the service — there is no configuration yet.

Skip to :ref:`setting-up-configuration` unless you are installing from source.

Installing from source instead
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Only if you are not using the package. Build a wheel and install the CLI to
``/usr/local/bin``, which is where the reference unit's ``ExecStart`` points:

.. code-block:: bash

   uv build
   sudo uv tool install ./dist/door_sync-*.whl

   sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/door-sync door-sync
   sudo mkdir -p /etc/door-sync /var/lib/door-sync /var/log/door-sync
   sudo chown -R door-sync:door-sync /var/lib/door-sync /var/log/door-sync

.. _setting-up-configuration:

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
   sudo chown door-sync:door-sync /var/log/door-sync /var/lib/door-sync

These paths are configurable in ``config.toml`` under ``[ops]``.

Installing the unit file
^^^^^^^^^^^^^^^^^^^^^^^^

The package installs the unit at ``/usr/lib/systemd/system/door-sync.service``
with ``ExecStart=/usr/bin/door-sync``; there is nothing to copy. Skip to
enabling it below.

For a source install, copy the reference unit from the repository — it points
``ExecStart`` at ``/usr/local/bin/door-sync``, matching ``uv tool install``:

.. code-block:: bash

   sudo cp deploy/door-sync.service /etc/systemd/system/

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

The package installs this at ``/etc/logrotate.d/door-sync`` already. For a
source install, copy the reference config from the repository:

.. code-block:: bash

   sudo cp deploy/door-sync.logrotate /etc/logrotate.d/door-sync

The logrotate config contents:

.. literalinclude:: ../deploy/door-sync.logrotate


Installing cloudflared
^^^^^^^^^^^^^^^^^^^^^^

Only needed if the webhook receiver is enabled. The receiver binds to loopback
and is reached through a Cloudflare Tunnel, so CiviCRM deliveries arrive via
``cloudflared`` rather than an open inbound port.

Install from Cloudflare's official ``.deb`` rather than their apt repository:
the repo floats, and a pinned package is what the appliance image installs too,
so both paths run the same binary. The package installs to
``/usr/bin/cloudflared`` and ships no unit of its own.

.. code-block:: bash

   curl -fsSLo cloudflared.deb \
     https://github.com/cloudflare/cloudflared/releases/download/2026.9.1/cloudflared-linux-arm64.deb
   echo "2a870d5bf6ea74d16c0923b804eabbf4943f1fd7c63a5c20fd41cc66b629c725  cloudflared.deb" \
     | sha256sum -c -
   sudo apt install ./cloudflared.deb
   cloudflared --version

The checksum is the same pin the appliance layer verifies
(``deploy/rpi-image-gen/layer/cloudflared.yaml``). Change the version and the
checksum together; they are per-release.

Create the service account and the config directory. ``cloudflared`` must be
able to read its own credentials, so the directory is owned by it:

.. code-block:: bash

   sudo useradd --system --shell /usr/sbin/nologin --no-create-home cloudflared
   sudo install -d -m0750 -o cloudflared -g cloudflared /etc/cloudflared

Create the tunnel and place ``config.yml`` and the credentials JSON in
``/etc/cloudflared/``, following Cloudflare's tunnel documentation. Point the
ingress rule at the webhook's loopback address — ``127.0.0.1:8787`` by default,
matching ``webhook.port`` in ``config.toml``.

A reference unit is provided at ``deploy/cloudflared.service``. It runs the
tunnel as the dedicated ``cloudflared`` account under ``NoNewPrivileges``,
``ProtectSystem=strict`` and ``ProtectHome``, rather than as root:

.. code-block:: bash

   sudo cp deploy/cloudflared.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cloudflared

.. note::

   Do not use ``cloudflared service install``. It generates a unit at run time
   that runs the tunnel as **root** and expects config at
   ``$HOME/.cloudflared/config.yml``. Passing ``--config /etc/cloudflared/config.yml``
   is Cloudflare's own documented override, so the unit here is declarative
   rather than generated, and unprivileged.

If webhook deliveries stop arriving, check the tunnel first — door-sync's logs
will say nothing at all, because the request never reaches it:

.. code-block:: bash

   sudo systemctl status cloudflared
   sudo journalctl -u cloudflared -n 50
