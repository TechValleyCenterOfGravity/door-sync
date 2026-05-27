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
