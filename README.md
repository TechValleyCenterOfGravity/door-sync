# door-sync
 
CiviCRM-to-UniFi-Access reconciliation service for door access control. Runs on a Raspberry Pi as a systemd service.
 
**In active development.** Pure modules, CiviCRM client, UniFi Access client, orchestrator + ops stubs, and the scheduler daemon loop are merged. A real alert transport (SMTP/webhook) is the remaining slice before v1.
 
## Docs

- [Architecture Reference](docs/architecture.md) — internal module layout, data contracts, pure/impure boundaries, coding conventions

## Dev setup
 
Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
 
```bash
uv sync                            # install
uv run pytest                      # tests
uv run pyrefly check               # type check
uv run ruff check .                # lint
```
 
## Running
 
```bash
uv run door-sync run                      # daemon: loop until SIGTERM/SIGINT
uv run door-sync run --dry-run            # daemon, but compute-only (no UniFi writes)
uv run door-sync run --once               # one reconcile cycle, then exit
uv run door-sync run --once --dry-run     # one cycle, compute-only
uv run door-sync show-diff                # read-only: print the computed diff
uv run door-sync validate-config          # load config, print issues, exit 0/1
```
 
Dry-run is safe to point at production data.

## Deploying on the Pi

`deploy/door-sync.service` is a systemd unit template. To install:

1. Install the CLI: `uv tool install --from /path/to/door-sync-checkout door-sync` (or build a wheel with `uv build` and run `uv tool install ./dist/door_sync-*.whl` on the Pi).
2. Create the service user: `sudo useradd --system --no-create-home door-sync`.
3. Create the config and ops directories:
   ```bash
   sudo mkdir -p /etc/door-sync /var/log/door-sync /var/lib/door-sync /var/run/door-sync
   sudo chown -R door-sync:door-sync /var/log/door-sync /var/lib/door-sync /var/run/door-sync
   ```
4. Drop `config.toml` into `/etc/door-sync/` (mode 0644) and `env` into the same dir (mode 0400).
5. Install the unit: `sudo cp deploy/door-sync.service /etc/systemd/system/`.
6. Start: `sudo systemctl daemon-reload && sudo systemctl enable --now door-sync`.

Stop with `sudo systemctl stop door-sync`; the daemon catches SIGTERM, finishes its in-flight reconcile, and exits 0.
 
## Configuration
 
Two files:
 
- **Env file** for secrets (API keys only). Dev: `.env`. Prod: `/etc/door-sync/env`, mode `0400`.
- **TOML file** for everything else (host URLs, TLS fingerprint, tier mapping, thresholds, cadence). Dev: `config.toml`. Prod: `/etc/door-sync/config.toml`.

See `config.example.toml` and `.env.example` for the full schema. Copy them and fill in real values.
 
## Project layout
 
```
src/door_sync/
  config.py         # env + TOML loading
  models.py         # dataclasses
  tier_mapping.py   # resolution rules (PURE)
  reconciler.py     # diff computation (PURE)
  safety.py         # guards (PURE)
  civicrm/          # API client
  unifi/            # API client (read + write + dry-run)
  orchestrator.py   # reconcile() — the wiring
  audit.py          # append-only JSONL log
  state.py          # last-success/halt JSON file
  alert.py          # flag-file alert stub (SMTP/webhook TBD)
  cli.py            # pretty-printers for show-diff / validate-config
  __main__.py       # argparse + subcommand dispatch
  scheduler.py      # daemon loop with SIGTERM/SIGINT handling
```
 
## License
 
TBD.