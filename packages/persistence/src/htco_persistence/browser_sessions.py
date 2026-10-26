"""The Browser Controller's Postgres side (slice 8A.2): profile locks and the session log.

`PostgresProfileLocks` holds a session-level advisory lock per environment on a **dedicated**
connection (outside the pool) for the whole browser session. It polls `pg_try_advisory_lock`
until the caller's deadline, so it never blocks a pooled connection or waits past the deadline.
If the worker dies, the connection drops and Postgres releases the lock (AC-8A.2.2).

`PostgresSessionLog` writes `app.browser_sessions` and the environment's `error_count` and
`last_session_at`, on the browser worker's role inside `tenant_scope(organization_id)`.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import anyio
import psycopg

from htco_application.browser_controller import SessionOutcome, SessionRecord
from htco_persistence.database import Database

POLL_SECONDS = 0.2


def lock_key(environment_id: uuid.UUID) -> str:
    return f"htco.browser_profile:{environment_id}"


class PostgresProfileLocks:
    def __init__(self, database: Database, *, poll_seconds: float = POLL_SECONDS) -> None:
        self._database = database
        self._poll = poll_seconds
        self._held: dict[uuid.UUID, psycopg.Connection[Any]] = {}

    @asynccontextmanager
    async def hold(self, environment_id: uuid.UUID, *, until: datetime) -> AsyncIterator[bool]:
        connection = await anyio.to_thread.run_sync(self._database.dedicated_connection)
        try:
            held = await self._wait(connection, lock_key(environment_id), until)
            if held:
                self._held[environment_id] = connection
            try:
                yield held
            finally:
                self._held.pop(environment_id, None)
                if held:
                    with anyio.CancelScope(shield=True):
                        await anyio.to_thread.run_sync(
                            _unlock, connection, lock_key(environment_id)
                        )
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(connection.close)

    async def still_held(self, environment_id: uuid.UUID) -> bool:
        """Asked on the lock's own connection: a dropped connection has lost the lock."""
        connection = self._held.get(environment_id)
        if connection is None:
            return False
        try:
            return await anyio.to_thread.run_sync(_holds, connection, lock_key(environment_id))
        except psycopg.Error:
            return False

    async def _wait(self, connection: psycopg.Connection[Any], key: str, until: datetime) -> bool:
        while True:
            if await anyio.to_thread.run_sync(_try_lock, connection, key):
                return True
            left = (until - datetime.now(UTC)).total_seconds()
            if left <= 0:
                return False
            await anyio.sleep(min(self._poll, left))


def _try_lock(connection: psycopg.Connection[Any], key: str) -> bool:
    row = connection.execute(
        "select pg_try_advisory_lock(hashtextextended(%s, 0))", [key]
    ).fetchone()
    return bool(row and row[0])


def _unlock(connection: psycopg.Connection[Any], key: str) -> None:
    with contextlib.suppress(psycopg.OperationalError):  # gone, and its lock with it
        connection.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", [key])


def _holds(connection: psycopg.Connection[Any], key: str) -> bool:
    # A bigint advisory key is stored as two 32-bit halves (classid high, objid low).
    row = connection.execute(
        """
        with k as (select hashtextextended(%s, 0) as key)
        select exists (
          select from pg_locks, k
          where locktype = 'advisory' and granted and objsubid = 1 and pid = pg_backend_pid()
            and classid = ((k.key >> 32) & 4294967295)::oid
            and objid = (k.key & 4294967295)::oid
        )
        """,
        [key],
    ).fetchone()
    return bool(row and row[0])


class PostgresSessionLog:
    def __init__(self, database: Database) -> None:
        self._database = database

    def started(self, record: SessionRecord) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                insert into app.browser_sessions
                  (id, organization_id, shop_id, environment_id, purpose, started_at)
                values (%s, %s, %s, %s, %s, %s)
                """,
                [
                    record.id,
                    record.organization_id,
                    record.shop_id,
                    record.environment_id,
                    record.purpose,
                    record.started_at,
                ],
            )

    def ended(
        self,
        record: SessionRecord,
        *,
        outcome: SessionOutcome,
        failure: str | None,
        at: datetime,
        count_error: bool,
    ) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                update app.browser_sessions set outcome = %s, failure = %s, ended_at = %s
                where id = %s and outcome = 'running'
                """,
                [outcome.value, failure, at, record.id],
            )
            connection.execute(
                """
                update app.shop_browser_environments
                set last_session_at = %s, error_count = error_count + %s
                where id = %s
                """,
                [at, 1 if count_error else 0, record.environment_id],
            )
