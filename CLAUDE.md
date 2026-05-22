# door-sync — agent guide

CiviCRM → UniFi Access reconciliation daemon. Runs on a Raspberry Pi under systemd.

**Status: pre-implementation.** Module skeleton exists in `src/door_sync/` but reconciler/safety/clients are not yet written. Architecture is locked; see `docs/architecture.md` before adding code.

## Commands

```bash
uv sync                       # install
uv run pytest                 # tests
uv run mypy src tests         # type check (strict)
uv run ruff check .           # lint
uv run door-sync --once       # one reconcile cycle, exit
uv run door-sync --dry-run    # compute + log diff; no UniFi writes
```

All tooling goes through `uv run` — the venv is managed by uv, not pip.

## Architecture

`docs/architecture.md` is authoritative for module layout, data contracts, and the pure/impure boundary. Read it before designing changes that cross module boundaries.

## Hard rules (from architecture doc — do not violate without a human in the loop)

- **No asyncio.** Sync `httpx` only. Do not "modernize" to async; the design rejects it deliberately (architecture.md §3).
- **Pure modules stay pure.** `reconciler.py`, `safety.py`, `tier_mapping.py` take dataclasses, return dataclasses. No logging, no config lookups, no HTTP, no exceptions on data issues — return a sentinel instead (architecture.md §5).
- **Frozen dataclasses.** All domain models in `models.py` are `@dataclass(frozen=True)`. Never mutate; construct a new instance.
- **Strict layering.** Nothing imports `orchestrator` except `scheduler` and (future) `webhook`. See dependency table in architecture.md §4.
- **Card ID redaction.** Logs show last-4 only. Never log a full card ID at any level (architecture.md §11).
- **Dry-run is sacred.** Dry-run flips a flag inside `UnifiClient` that turns writes into no-ops. Pure modules behave identically in dry-run and live — do not branch on dry-run in pure code.
- **Fail-secure on safety guards.** Any guard firing means zero writes that cycle. No partial application.

## Testing

- Pure-module tests use plain dataclass construction — no mocks, no HTTP fixtures.
- Idempotency canary: `compute_diff` immediately after `unifi.apply()` must yield all-empty diff sets. Include this test for the reconciler (architecture.md §8).

## Config

Two-file split: secrets in env (`.env` dev, `/etc/door-sync/env` prod, mode 0400), everything else in TOML (`config.toml` dev, `/etc/door-sync/config.toml` prod). Schema is not yet implemented.

# Python Package Management with uv
Use uv exclusively for Python package management in this project.
## Package Management Commands
- All Python dependencies **must be installed, synchronized, and locked** using uv
- Never use pip, pip-tools, poetry, or conda directly for dependency management
Use these commands:
- Install dependencies: `uv add <package>`
- Remove dependencies: `uv remove <package>`
- Sync environment: `uv sync`
- Lock dependencies: `uv lock`
## Running Python Code
- Run a Python script with `uv run <script-name>.py`
- Run Python tools with `uv run <tool>` (e.g. `uv run pytest`, `uv run ruff`, `uv run mypy`, `uv run pre-commit`)
- Launch a Python REPL with `uv run python`
