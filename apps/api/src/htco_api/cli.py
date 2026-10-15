"""The operator command line: `htco admin bootstrap` (slice 2.5), `htco admin rotate-token-keys`
(slice 4.2).

Usage, once the owner has signed up through Supabase Auth:

    htco admin bootstrap --email owner@example.com --organization "HT Commerce" \
        --display-name "Owner"

It reads `HTCO_DATABASE_URL`, creates the organization and the owner membership (or finds them
when they already exist) and prints the result as one JSON line. Exit codes follow sysexits.h:
65 for input the command cannot use (including an email nobody signed up with), 78 for missing
configuration, 2 for usage errors.

After a new Etsy token vault key is deployed (with the old one among the previous keys):

    htco admin rotate-token-keys [--batch-size 100]

It reads `HTCO_DATABASE_URL`, `HTCO_TOKEN_VAULT_KEY` and `HTCO_TOKEN_VAULT_PREVIOUS_KEYS`,
re-wraps every row still on a previous key with the current key while the API keeps serving, and
prints the count and the rows per key ID; when only the current ID is left, the previous keys can
be removed.
"""

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import Protocol, TextIO

import anyio
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from htco_api.settings import check_token_vault_keys, token_key_ring
from htco_application.bootstrap import (
    BootstrapRequest,
    BootstrapStore,
    UserNotFoundError,
    bootstrap_owner,
)
from htco_persistence.bootstrap import PostgresBootstrapStore
from htco_persistence.database import Database
from htco_persistence.shop_integrations import PostgresTokenVault
from htco_platform.crypto import EnvelopeSealer, KeyRing
from htco_platform.settings import ConfigurationError, load_settings

EXIT_DATA_ERROR = 65
EXIT_CONFIGURATION_ERROR = 78


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTCO_", frozen=True, extra="ignore")

    database_url: SecretStr


class RotationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HTCO_", frozen=True, extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr
    token_vault_key: SecretStr
    token_vault_previous_keys: SecretStr | None = None

    @field_validator("token_vault_previous_keys", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _keys_are_valid(self) -> RotationSettings:
        check_token_vault_keys(self.token_vault_key, self.token_vault_previous_keys)
        return self

    @property
    def key_ring(self) -> KeyRing:
        ring = token_key_ring(self.token_vault_key, self.token_vault_previous_keys)
        if ring is None:  # pragma: no cover - the key is required above
            raise ValueError("HTCO_TOKEN_VAULT_KEY is required")
        return ring


class ManagedRotator(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def rotate_keys(self, *, batch_size: int) -> int: ...

    async def key_usage(self) -> dict[str, int]: ...


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


class _DatabaseTokenVault(PostgresTokenVault):
    def __init__(self, database: Database, ring: KeyRing) -> None:
        super().__init__(database, EnvelopeSealer(ring))
        self._managed = database

    async def start(self) -> None:
        await self._managed.start()

    async def stop(self) -> None:
        await self._managed.stop()


def _postgres_rotator(url: str, ring: KeyRing) -> ManagedRotator:
    return _DatabaseTokenVault(Database(url, min_size=1, max_size=1), ring)


MAX_BATCH_SIZE = 10_000


def _batch_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_BATCH_SIZE}")
    return size


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
    rotate = commands.add_parser(
        "rotate-token-keys",
        help="Re-wrap stored Etsy tokens with the current vault key (safe while the API runs).",
    )
    rotate.add_argument(
        "--batch-size", type=_batch_size, default=100, help="Rows per transaction (default 100)."
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    new_store: Callable[[str], ManagedStore] = _postgres_store,
    new_rotator: Callable[[str, KeyRing], ManagedRotator] = _postgres_rotator,
    stdout: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out = stdout or sys.stdout
    if args.command == "rotate-token-keys":
        return _rotate(args.batch_size, new_rotator, out)
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


def _rotate(
    batch_size: int, new_rotator: Callable[[str, KeyRing], ManagedRotator], out: TextIO
) -> int:
    try:
        settings = load_settings(RotationSettings)
    except ConfigurationError as error:
        sys.stderr.write(f"htco: invalid configuration\n{error}\n")
        return EXIT_CONFIGURATION_ERROR
    ring = settings.key_ring
    rotator = new_rotator(settings.database_url.get_secret_value(), ring)

    async def run() -> int:
        await rotator.start()
        try:
            rotated = await rotator.rotate_keys(batch_size=batch_size)
            usage = await rotator.key_usage()
        finally:
            await rotator.stop()
        out.write(
            json.dumps(
                {
                    "rotated": rotated,
                    "current_key_id": ring.current_key_id,
                    "rows_by_key_id": usage,
                }
            )
            + "\n"
        )
        return 0

    return anyio.run(run)


def entry_point() -> None:  # pragma: no cover - the console script
    sys.exit(main())
