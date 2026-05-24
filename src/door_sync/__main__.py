"""door-sync CLI entry point.

Subcommands:
  run [--once] [--dry-run]  Run daemon loop (default) or one cycle (--once).
  show-diff                 Read-only: fetch + compute diff, pretty-print, exit.
  validate-config           Load config, print issues, exit 0 (ok) or 1 (bad).

Exit codes:
  0  success
  1  cycle halted by safety guards; config validation failed
  2  cycle crashed (exception escaped orchestrator); show-diff fetch failed
 64  CLI usage error (argparse default)
"""

import argparse
import logging
import sys
from pathlib import Path

from door_sync import cli, orchestrator, reconciler, scheduler, tier_mapping
from door_sync import config as config_mod
from door_sync.civicrm.client import CivicrmClient
from door_sync.unifi.client import UnifiClient

# Expose config_mod so tests can monkeypatch it via main_mod.config_mod.
__all__ = ["config_mod", "CivicrmClient", "UnifiClient", "scheduler", "main"]

_logger = logging.getLogger("door_sync")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(verbose=args.verbose)

    if args.subcommand == "run":
        return cmd_run(args)
    if args.subcommand == "show-diff":
        return cmd_show_diff(args)
    if args.subcommand == "validate-config":
        return cmd_validate_config(args)
    parser.print_help(sys.stderr)
    return 64


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="door-sync")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (default: $DOOR_SYNC_CONFIG_DIR/config.toml or ./config.toml)",
    )
    p.add_argument(
        "--env-file",
        dest="env_file",
        type=Path,
        default=None,
        help="Path to env file (default: $DOOR_SYNC_CONFIG_DIR/env or ./.env)",
    )

    sub = p.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser("run", help="Execute reconciliation cycles")
    run_p.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (REQUIRED for now)",
    )
    run_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Compute diff and log to audit but do not write to UniFi",
    )

    sub.add_parser("show-diff", help="Read-only: print computed diff and exit")
    sub.add_parser(
        "validate-config",
        help="Load config and print issues; exit 0 (ok) or 1 (bad)",
    )

    return p


def _setup_logging(*, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = config_mod.load(config_path=args.config, env_path=args.env_file)
    except config_mod.ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1

    if not args.once:
        return scheduler.run_forever(config, dry_run=args.dry_run)

    try:
        result = orchestrator.reconcile(config, dry_run=args.dry_run)
    except Exception as exc:
        orchestrator.handle_crash(exc, paths=config.ops_paths)
        return 2

    return 1 if result.halted else 0


def cmd_show_diff(args: argparse.Namespace) -> int:
    try:
        config = config_mod.load(config_path=args.config, env_path=args.env_file)
    except config_mod.ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1

    try:
        with (
            CivicrmClient(config.civicrm) as civicrm,
            UnifiClient(config.unifi, dry_run=True) as unifi,
        ):
            members = civicrm.fetch_active()
            resolved = [tier_mapping.resolve(m, config.tier_mapping) for m in members]
            users = unifi.fetch_users()
            diff = reconciler.compute_diff(resolved, users)
    except Exception:
        _logger.exception("show-diff failed")
        return 2

    cli.print_diff(diff, file=sys.stdout)
    return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        config_mod.load(config_path=args.config, env_path=args.env_file)
    except config_mod.ConfigError as e:
        cli.print_config_issues(e.issues, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
