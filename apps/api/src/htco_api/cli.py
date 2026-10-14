"""The operator command line: `htco admin bootstrap` (slice 2.5).

Usage, once the owner has signed up through Supabase Auth:

    htco admin bootstrap --email owner@example.com --organization "HT Commerce" \
        --display-name "Owner"

It reads `HTCO_DATABASE_URL`, creates the organization and the owner membership (or finds them
when they already exist) and prints the result as one JSON line. Exit codes follow sysexits.h:
65 for input the command cannot use (including an email nobody signed up with), 78 for missing
configuration, 2 for usage errors.
"""

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import Protocol, TextIO

import anyio
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from htco_application.bootstrap import (
    BootstrapRequest,
    BootstrapStore,
    UserNotFoundError,
    bootstrap_owner,
)
from htco_persistence.bootstrap import PostgresBootstrapStore
from htco_persistence.database import Database
from htco_platform.settings import ConfigurationError, load_settings

EXIT_DATA_ERROR = 65
EXIT_CONFIGURATION_ERROR = 78


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTCO_", frozen=True, extra="ignore")

    database_url: SecretStr


class ManagedStore(BootstrapStore, Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _DatabaseBootstrapStore(PostgresBootstrapStore):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self._database = database

    async def start(self) -> None:
        await self._database.start()

    async def stop(self) -> None:
        await self._database.stop()


def _postgres_store(url: str) -> ManagedStore:
    return _DatabaseBootstrapStore(Database(url, min_size=1, max_size=1))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="htco", description="HTCO operator commands.")
    groups = parser.add_subparsers(dest="group", required=True)
    admin = groups.add_parser("admin", help="Administration of organizations and owners.")
    commands = admin.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser(
        "bootstrap", help="Create the owner's organization and membership (idempotent)."
    )
    bootstrap.add_argument("--email", required=True, help="The owner's sign-up email.")
    bootstrap.add_argument("--organization", required=True, help="The organization's name.")
    bootstrap.add_argument("--display-name", required=True, help="The owner's display name.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    new_store: Callable[[str], ManagedStore] = _postgres_store,
    stdout: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out = stdout or sys.stdout
    try:
        settings = load_settings(AdminSettings)
    except ConfigurationError as error:
        sys.stderr.write(f"htco: invalid configuration\n{error}\n")
        return EXIT_CONFIGURATION_ERROR
    try:
        request = BootstrapRequest(
            email=args.email, organization_name=args.organization, display_name=args.display_name
        )
    except ValueError as error:
        sys.stderr.write(f"htco: {error}\n")
        return EXIT_DATA_ERROR

    store = new_store(settings.database_url.get_secret_value())

    async def run() -> int:
        await store.start()
        try:
            result = await bootstrap_owner(store, request, correlation_id=str(uuid.uuid7()))
        except UserNotFoundError:
            sys.stderr.write(
                "htco: no user has signed up with this email; sign up first, then run again\n"
            )
            return EXIT_DATA_ERROR
        finally:
            await store.stop()
        out.write(
            json.dumps(
                {
                    "created": result.created,
                    "organization_id": str(result.organization_id),
                    "membership_id": str(result.membership_id),
                }
            )
            + "\n"
        )
        return 0

    return anyio.run(run)


def entry_point() -> None:  # pragma: no cover - the console script
    sys.exit(main())
