"""Command line entry points."""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace

from dreeve_garmin_connector.auth import Authenticator
from dreeve_garmin_connector.config import Config, InvalidConfiguration
from dreeve_garmin_connector.delivery import WatchFolder, WatchFolderUnusable
from dreeve_garmin_connector.garmin import GarminConnectClient, GarminError
from dreeve_garmin_connector.ledger import LEDGER_FILENAME, CorruptLedger, Ledger
from dreeve_garmin_connector.logging_ import Secrets, configure_logging
from dreeve_garmin_connector.sync import Sync, SystemClock

EXIT_OK = 0
EXIT_FAILED = 1

logger = logging.getLogger(__name__)


class MfaPromptUnavailable(Exception):
    """Garmin asked for a code and there is nobody at the keyboard to type it."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dreeve-garmin-connector",
        description="Syncs Garmin Connect activities into Dreeve's watch folder.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Log in to Garmin once, interactively, and store the session.")
    sync_command = commands.add_parser("sync-once", help="Run a single sync cycle and exit.")
    sync_command.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded, download nothing and leave the ledger untouched.",
    )

    arguments = parser.parse_args(argv)

    try:
        config = Config.from_env(os.environ)
    except InvalidConfiguration as exception:
        # Logging is configured from the very config that just failed, so this one goes straight to stderr.
        print(exception, file=sys.stderr)
        return EXIT_FAILED

    if getattr(arguments, "dry_run", False):
        config = replace(config, dry_run=True)

    secrets = Secrets([config.garmin_email, config.garmin_password])
    configure_logging(config.log_level, config.log_format, secrets)

    # argparse has already rejected anything that is not a registered command.
    commands_by_name = {"login": login, "sync-once": sync_once}

    return commands_by_name[arguments.command](config, secrets)


def login(config: Config, secrets: Secrets) -> int:
    try:
        Authenticator(config, secrets).log_in(prompt_mfa=ask_for_mfa_code)
    except (InvalidConfiguration, GarminError, MfaPromptUnavailable) as exception:
        logger.error("Login failed. %s", exception)
        return EXIT_FAILED

    logger.info(
        "Logged in. The session is stored in %s and will be resumed from there; "
        "keep that volume and you should not have to do this again.",
        config.garmin_tokens,
    )

    return EXIT_OK


def sync_once(config: Config, secrets: Secrets) -> int:
    try:
        result = build_sync(config, secrets).run_once()
    except (InvalidConfiguration, GarminError, CorruptLedger, WatchFolderUnusable) as exception:
        logger.error("Sync failed. %s", exception)
        return EXIT_FAILED

    logger.info("Cycle finished: %s", result)

    return EXIT_OK


def build_sync(config: Config, secrets: Secrets) -> Sync:
    watch_folder = WatchFolder(config.watch_dir, config.on_conflict)
    watch_folder.prepare()

    session = Authenticator(config, secrets).resume()

    return Sync(
        config=config,
        ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
        client=GarminConnectClient(session),
        watch_folder=watch_folder,
        clock=SystemClock(),
    )


def ask_for_mfa_code() -> str:
    if not sys.stdin.isatty():
        raise MfaPromptUnavailable(
            "Garmin asked for a multi-factor code, but there is no terminal to ask on. "
            "Run this with `docker compose run --rm connector login`, which attaches one."
        )

    return input("Garmin sent a multi-factor code. Enter it: ").strip()
