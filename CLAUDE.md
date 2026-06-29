# door-sync — agent guide

CiviCRM → UniFi Access reconciliation daemon. Runs on a Raspberry Pi under systemd.

**Status: in active development.** Pure modules, CiviCRM client, UniFi Access client, orchestrator + ops (audit JSONL, state JSON, alert with flag-file + SMTP/Mailgun transports), and the scheduler daemon loop (SIGTERM/SIGINT handling) are merged. Architecture is locked; see `docs/architecture.md` before adding code.

## Commands

```bash
uv sync                                       # install
uv run pytest                                 # tests
uv run pyrefly check                          # type check
uv run ruff check .                           # lint
uv run door-sync run --once                   # one reconcile cycle, exit
uv run door-sync run --once --dry-run         # compute + log diff; no UniFi writes
uv run door-sync show-diff                    # read-only: print computed diff
uv run door-sync validate-config              # load config, print issues, exit
```

All tooling goes through `uv run` — the venv is managed by uv, not pip.

## Architecture

`docs/architecture.md` is authoritative for module layout, data contracts, and the pure/impure boundary. Read it before designing changes that cross module boundaries.

## Hard rules (from architecture doc — do not violate without a human in the loop)

- **No asyncio.** Sync `httpx` only. Do not "modernize" to async; the design rejects it deliberately (architecture.md §3).
- **CivicrmClient / UnifiClient as context managers.** Both own an `httpx.Client` and must be used inside `with` blocks (or `close()` in `finally`) to avoid socket/FD leaks. The orchestrator and CLI handlers already do this — match the pattern.
- **Don't mix `import X` and `from X import Y` for the same module.** CodeQL flags it post-PR; local `ruff`/`pyrefly` don't. Pick one style per file.
- **Pure modules stay pure.** `reconciler.py`, `safety.py`, `tier_mapping.py` take dataclasses, return dataclasses. No logging, no config lookups, no HTTP, no exceptions on data issues — return a sentinel instead (architecture.md §5).
- **Frozen dataclasses.** All domain models in `models.py` are `@dataclass(frozen=True)`. Never mutate; construct a new instance.
- **Strict layering.** Nothing imports `orchestrator` except `scheduler` and (future) `webhook`. See dependency table in architecture.md §4.
- **Card ID redaction.** Logs show last-4 only. Never log a full card ID at any level (architecture.md §11).
- **No member names in logs/alerts.** Operational and audit log streams identify members by `contact_id` (and, for unmanaged UniFi accounts, user id) — never by name. CodeQL's `py/clear-text-logging-sensitive-data` flags `display_name` as PII. The interactive `show-diff` CLI may print names (direct operator output, not a log). See architecture/conventions.rst.
- **Dry-run is sacred.** Dry-run flips a flag inside `UnifiClient` that turns writes into no-ops. Pure modules behave identically in dry-run and live — do not branch on dry-run in pure code.
- **Fail-secure on safety guards.** Any guard firing means zero writes that cycle. No partial application.
- **Crash logging uses `_logger.error("...", exc_info=exc)`, not `_logger.exception(...)`.** `exception()` reads `sys.exc_info()`, which is `(None, None, None)` outside an active `except` clause — the traceback would be silently dropped. Python 3.5+ accepts an exception instance directly via `exc_info=`. See `orchestrator.handle_crash` for the reference impl.

## Testing

- Pure-module tests use plain dataclass construction — no mocks, no HTTP fixtures.
- Idempotency canary: `compute_diff` immediately after `unifi.apply()` must yield all-empty diff sets. Include this test for the reconciler (architecture.md §8).

## IDE diagnostics

- pyrefly is the authoritative type checker; Pyright in the IDE lags on new packages and false-flags `__exit__` protocol args / underscore-prefixed unused params. When `uv run pyrefly check`, `ruff check`, and `pytest` are all green, trust them.

## PR review findings (Copilot, CodeQL, etc.)

Before acting on a bot finding, read the flagged code and confirm the issue is real. Bots have false positives and sometimes propose suboptimal fixes — verify the flagged lines and surrounding context first, then decide whether to apply their suggestion, apply a different fix, or push back.

Recurring true-positive: CodeQL `py/ineffectual-statement` flags `...` (Ellipsis) in `Protocol` method bodies. Use a docstring instead — it's exempt from the rule and documents the contract. Example: `def __call__(self, …) -> X: """One-line description."""`

## Config

Two-file split: secrets in env (`.env` dev, `/etc/door-sync/env` prod, mode 0400), everything else in TOML (`config.toml` dev, `/etc/door-sync/config.toml` prod). Schema is not yet implemented.

# Python Project Rules

<!-- Generated from pydevtools.com, the Python Developer Tooling Handbook -->
<!-- Last verified against: uv 0.11.15, ruff 0.15.13, pyrefly 1.0.0, ty 0.0.38, pytest 9.0.3 -->
<!-- Full explanations: https://pydevtools.com/handbook/explanation/modern-python-project-setup-guide-for-ai-assistants/ -->

When working with Python, invoke the relevant /astral:<skill> for uv, and ruff to ensure best practices are followed.

## Package management

This project uses uv. Do not use pip, pip-tools, poetry, or conda.

- Add runtime dependency: `uv add <package>` (writes to `[project.dependencies]`)
- Add dev dependency: `uv add --dev <package>` (writes to `[dependency-groups]` per PEP 735)
- Remove dependency: `uv remove <package>`
- Sync environment from lockfile: `uv sync`
- Regenerate lockfile from constraints: `uv lock`
- Upgrade locked versions: `uv lock --upgrade`
- Commit `uv.lock` to version control (current uv guidance is to commit it for applications, CLIs, and libraries)

## Running code

Always use `uv run` to execute Python code and tools. Never call `python`, `pytest`, `ruff`, or other tools directly. They may not resolve to the project's virtual environment.

- Run a script: `uv run python script.py`
- Run a module: `uv run python -m module_name`
- Run a tool: `uv run pytest`, `uv run ruff check .`
- One-off tool (not a project dependency): `uvx <tool>`

## Creating new projects

- Application or script: `uv init project-name`
- Library or distributable package: `uv init --package project-name`
- Always use `pyproject.toml` for metadata (PEP 621). Never create `setup.py`, `setup.cfg`, or `requirements.txt`.

## Testing

- Framework: pytest
- Run tests: `uv run pytest`
- Test files go in `tests/` at the project root
- Test file naming: `test_*.py`
- Test function naming: `test_*`
- No `__init__.py` needed in `tests/`

## Linting and formatting

- Tool: ruff (handles both linting and formatting)
- Lint: `uv run ruff check .`
- Lint and auto-fix: `uv run ruff check --fix .`
- Format: `uv run ruff format .`
- Check formatting: `uv run ruff format --check .`
- Configuration lives in `pyproject.toml` under `[tool.ruff]`

## Type checking

- Tool: pyrefly (preferred), ty, or mypy
- Run: `uv run pyrefly check`, `uv run ty check`, or `uv run mypy .`
- Configuration lives in `pyproject.toml` under `[tool.pyrefly]`, `[tool.ty]`, or `[tool.mypy]`

## Code style

- Follow ruff's defaults for formatting (88 char line length, double quotes, spaces)
- Import sorting is handled by ruff (`isort` rules enabled via `select = ["I"]`)
- Do not add `# type: ignore` comments without an error code

## Pre-commit hooks

- Tool: prek (preferred) or pre-commit
- Install prek hooks: `uvx prek install`
- Install pre-commit hooks: `uvx pre-commit install`
- Do not install pre-commit or prek with pip. Use `uvx`.

## What NOT to do

- Do not create or activate virtual environments manually. uv manages `.venv/` automatically.
- Do not install packages globally or with `pip install`.
- Do not create `requirements.txt` for dependency management. Use `pyproject.toml` and `uv.lock`.
- Do not run `python setup.py` commands.
- Do not add dependencies to pyproject.toml by hand. Use `uv add`.
- If you must edit pyproject.toml directly, write dev dependencies under `[dependency-groups]` (PEP 735), not the legacy `[tool.uv.dev-dependencies]` table.