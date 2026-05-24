# door-sync
 
CiviCRM-to-UniFi-Access reconciliation service for door access control. Runs on a Raspberry Pi as a systemd service.
 
**In active development.** Pure modules, CiviCRM client, UniFi Access client, orchestrator + ops stubs are merged. Scheduler and a real alert transport are the remaining slices before v1.
 
## Docs

- [Architecture Reference](docs/architecture.md) — internal module layout, data contracts, pure/impure boundaries, coding conventions

## Dev setup
 
Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
 
```bash
uv sync                            # install
uv run pytest                      # tests
uv run mypy --strict src tests     # type check (strict)
uv run ruff check .                # lint
```
 
## Running
 
```bash
uv run door-sync run --once               # one reconcile cycle, then exit
uv run door-sync run --once --dry-run     # compute and log the diff; make no UniFi writes
uv run door-sync show-diff                # read-only: print the computed diff
uv run door-sync validate-config          # load config, print issues, exit 0/1
```
 
Dry-run is safe to point at production data. Daemon mode (continuous scheduling) is not yet implemented — `run` without `--once` will exit with usage error 64 until the scheduler slice lands.
 
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
  scheduler.py      # daemon loop (NOT YET IMPLEMENTED)
```
 
## License
 
TBD.