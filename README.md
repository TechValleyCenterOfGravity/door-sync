# door-sync
 
CiviCRM-to-UniFi-Access reconciliation service for door access control. Runs on a Raspberry Pi as a systemd service.
 
**Pre-implementation.** Architecture decided; no production code yet.
 
## Docs

- [Architecture Reference](docs/architecture.md) — internal module layout, data contracts, pure/impure boundaries, coding conventions

## Dev setup
 
Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
 
```bash
uv sync                   # install
uv run pytest             # tests
uv run mypy src tests     # type check
uv run ruff check .       # lint
```
 
## Running
 
```bash
uv run door-sync               # live daemon on the configured schedule
uv run door-sync --once        # one reconcile cycle, then exit
uv run door-sync --dry-run     # compute and log the diff; make no UniFi writes
```
 
Dry-run is safe to point at production data.
 
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
  scheduler.py      # daemon loop
  audit.py          # JSON-lines log
  state.py          # last-success timestamp
  alert.py          # halt notifications
```
 
## License
 
TBD.