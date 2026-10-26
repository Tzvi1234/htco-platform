"""The sealed browser profiles on Postgres (slice 8A.1): `app.browser_profile_archives` and the
profile columns of `app.shop_browser_environments`, on the browser worker's role.

Use it on a `Database(role="htco_browser")` inside `tenant_scope(organization_id)`: the role's
policies show one organization's rows only, so an environment or archive of another
organization reads as missing and cannot be written (AC-8A.1.2). The archive column holds sealed
bytes only; this adapter never opens them.
"""

import uuid
from typing import Any

import psycopg

from htco_application.browser_profiles import (
    ProfileFenceLostError,
    ProfileRecordError,
    SealedArchive,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_persistence.browser_sessions import PostgresLease, holds_lock, lock_key
from htco_persistence.database import Database


def _environment(row: tuple[Any, ...]) -> BrowserEnvironment:
    return BrowserEnvironment(
        id=row[0], organization_id=row[1], shop_id=row[2], status=EnvironmentStatus(row[3]),
        challenge_count=row[4], last_health_at=row[5], last_health_ok=row[6],
    )  # fmt: skip


class PostgresBrowserProfiles:
    """`ProfileArchiveStore` and `ProfileRecords` for the browser worker; an archive and its
    record on the environment row are always written together."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select id, organization_id, shop_id, status, challenge_count, last_health_at,
                       last_health_ok
                from app.shop_browser_environments where id = %s
                """,
                [environment_id],
            ).fetchone()
        return None if row is None else _environment(row)

    def get(self, environment_id: uuid.UUID) -> SealedArchive | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select environment_id, organization_id, shop_id, key_id, archive, sealed_at
                from app.browser_profile_archives where environment_id = %s
                """,
                [environment_id],
            ).fetchone()
        if row is None:
            return None
        return SealedArchive(
            environment_id=row[0], organization_id=row[1], shop_id=row[2], key_id=row[3],
            blob=bytes(row[4]), sealed_at=row[5],
        )  # fmt: skip

    def put(self, archive: SealedArchive, *, fence: object | None = None) -> None:
        """The archive and the environment's record of it, in one transaction.

        With a fence (the session's `PostgresLease`), the transaction runs on the lease's own
        dedicated connection, the one that holds the profile's advisory lock, and first checks
        that this backend still holds it (#169). A session-level advisory lock is only released
        when its backend unlocks or exits, and an exiting backend never commits, so no other
        session can take the profile between that check and the commit: the check is part of
        the write. A lost lease raises `ProfileFenceLostError` and writes nothing."""
        if fence is None:
            with self._database.connection() as connection, connection.transaction():
                _write(connection, archive)
            return
        if (
            not isinstance(fence, PostgresLease)
            or not fence.held
            or fence.environment_id != archive.environment_id
        ):
            raise ProfileFenceLostError("the write's lease does not hold this profile")
        connection = fence.connection
        try:
            with self._database.transaction_on(connection):
                if not holds_lock(connection, lock_key(fence.environment_id)):
                    raise ProfileFenceLostError("the session's lease no longer holds the profile")
                _write(connection, archive)
        except psycopg.OperationalError, psycopg.InterfaceError:
            # The lock's connection is gone, and its lock with it: nothing was committed.
            raise ProfileFenceLostError("the profile lock's connection dropped") from None

    def delete(self, environment_id: uuid.UUID) -> None:
        """The archive and the record of it, cleared in one transaction."""
        with self._database.connection() as connection, connection.transaction():
            cleared = connection.execute(
                """
                update app.shop_browser_environments
                set profile_storage_key = null, profile_key_id = null
                where id = %s
                """,
                [environment_id],
            )
            if cleared.rowcount != 1:
                raise ProfileRecordError("no environment row to clear")
            connection.execute(
                "delete from app.browser_profile_archives where environment_id = %s",
                [environment_id],
            )


def _write(connection: psycopg.Connection[Any], archive: SealedArchive) -> None:
    """The archive and its record on the environment's row, inside the caller's transaction."""
    recorded = connection.execute(
        """
        update app.shop_browser_environments
        set profile_storage_key = %s, profile_key_id = %s
        where id = %s and organization_id = %s and shop_id = %s
        """,
        [
            archive.storage_key,
            archive.key_id,
            archive.environment_id,
            archive.organization_id,
            archive.shop_id,
        ],
    )
    if recorded.rowcount != 1:
        raise ProfileRecordError("no environment row to record the archive on")
    connection.execute(
        """
        insert into app.browser_profile_archives
          (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (environment_id) do update
          set key_id = excluded.key_id, archive = excluded.archive,
              sealed_at = excluded.sealed_at
        """,
        [
            archive.environment_id,
            archive.organization_id,
            archive.shop_id,
            archive.key_id,
            archive.blob,
            archive.sealed_at,
        ],
    )
