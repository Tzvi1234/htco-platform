"""The sealed browser profiles on Postgres (slices 8A.1 and 8A.2c), on the browser worker's role.

Since 8A.2c (AC-8A.2.23) the browser role has no right on `app.browser_profile_archives` and
none on the environment's profile columns. It opens, saves and deletes a profile only through
the `browser` schema's functions, with a `browser.*` job it leased: the database takes the
environment and the organization from that job row, never from here or from
`htco.organization_id`, and writes with the generation compare-and-set of ADR-0025 §1. This
adapter is therefore built for one leased job. Reading the environment's status still goes
through the role's policies inside `tenant_scope(organization_id)`. The archive column holds
sealed bytes only; this adapter never opens them.
"""

import uuid
from typing import Any

import psycopg

from htco_application.browser_dispatch import BrowserJobLease
from htco_application.browser_profiles import (
    NO_ARCHIVE,
    ProfileFenceLostError,
    ProfileRecordError,
    ProfileStaleError,
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
    """`ProfileArchiveStore` and `ProfileRecords` for one leased `browser.*` job; an archive and
    its record on the environment row are always written together, by the database function."""

    def __init__(self, database: Database, job: BrowserJobLease) -> None:
        self._database = database
        self._job = job

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
        """The leased job's own environment's archive; any other environment reads as missing."""
        try:
            with self._database.connection() as connection:
                row = connection.execute(
                    "select environment_id, organization_id, shop_id, key_id, archive, "
                    "sealed_at, generation from browser.open_profile(%s, %s)",
                    [self._job.job_id, self._job.token],
                ).fetchone()
        except psycopg.errors.InsufficientPrivilege:
            raise ProfileRecordError("the job's lease does not reach this profile") from None
        if row is None or row[0] != environment_id:
            return None
        return SealedArchive(
            environment_id=row[0], organization_id=row[1], shop_id=row[2], key_id=row[3],
            blob=bytes(row[4]), sealed_at=row[5], generation=row[6],
        )  # fmt: skip

    def put(
        self, archive: SealedArchive, *, fence: object | None = None, expected: int | None = None
    ) -> None:
        """The archive and the environment's record of it, in one transaction, through the
        leased job.

        With a fence (the session's `PostgresLease`), the call runs on the lease's own dedicated
        connection and first checks that this backend still holds the profile lock (#169): a
        lost lease raises `ProfileFenceLostError` and writes nothing. With `expected`, the
        database replaces only the generation the session read (ADR-0025 §1); a newer one raises
        `ProfileStaleError`. `None` provisions a profile that has no archive yet."""
        if fence is None:
            with self._database.connection() as connection, connection.transaction():
                self._save(connection, archive, expected)
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
                self._save(connection, archive, expected)
        except psycopg.OperationalError, psycopg.InterfaceError:
            # The lock's connection is gone, and its lock with it: nothing was committed.
            raise ProfileFenceLostError("the profile lock's connection dropped") from None

    def delete(self, environment_id: uuid.UUID) -> None:
        """The archive and the record of it, cleared in one transaction through the leased
        job; another environment than the job's is refused and changes nothing."""
        environment = self.by_id(environment_id)
        if environment is None or not self._owns(environment.organization_id, environment.shop_id):
            raise ProfileRecordError("not the leased job's environment")
        try:
            with self._database.connection() as connection, connection.transaction():
                connection.execute(
                    "select browser.delete_profile(%s, %s)", [self._job.job_id, self._job.token]
                )
        except psycopg.errors.InsufficientPrivilege:
            raise ProfileRecordError("the job's lease does not reach this profile") from None

    def _owns(self, organization_id: uuid.UUID, shop_id: uuid.UUID) -> bool:
        return organization_id == self._job.organization_id and shop_id == self._job.shop_id

    def _save(
        self, connection: psycopg.Connection[Any], archive: SealedArchive, expected: int | None
    ) -> None:
        if not self._owns(archive.organization_id, archive.shop_id):
            raise ProfileRecordError("not the leased job's environment")
        generation = None if expected is None else (0 if expected == NO_ARCHIVE else expected)
        try:
            with connection.transaction():  # a savepoint: a refusal leaves nothing behind
                connection.execute(
                    "select browser.save_profile(%s, %s, %s, %s, %s, %s, %s)",
                    [
                        self._job.job_id,
                        self._job.token,
                        archive.key_id,
                        archive.blob,
                        archive.sealed_at,
                        archive.storage_key,
                        generation,
                    ],
                )
        except psycopg.errors.SerializationFailure:
            raise ProfileStaleError("the archive changed since the session read it") from None
        except psycopg.errors.InsufficientPrivilege:
            raise ProfileRecordError("the job's lease does not reach this profile") from None
