"""Stored responses for `Idempotency-Key` (slice 5.2): `private.idempotency_keys`, login role only.

`begin` claims the key with one insert (the unique `(user_id, key)` makes concurrent first
requests have one winner) after dropping the key's expired row, and a few other expired rows so
the table does not grow.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import anyio
from psycopg.types.json import Jsonb

from htco_application.idempotency import (
    KEY_LIFETIME,
    Begin,
    IdempotentRequest,
    KeyReused,
    Replay,
    Started,
    StillRunning,
)
from htco_persistence.database import Database

_PRUNE_BATCH = 100


class PostgresIdempotencyStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def begin(self, request: IdempotentRequest, *, now: datetime) -> Begin:
        return await anyio.to_thread.run_sync(self._begin, request, now)

    def _begin(self, request: IdempotentRequest, now: datetime) -> Begin:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                delete from private.idempotency_keys
                where id in (
                  select id from private.idempotency_keys where expires_at <= %s limit %s
                ) or (user_id = %s and key = %s and expires_at <= %s)
                """,
                [now, _PRUNE_BATCH, request.user_id, request.key, now],
            )
            claimed = connection.execute(
                """
                insert into private.idempotency_keys
                  (id, user_id, key, method, path, request_hash, state, created_at, expires_at)
                values (gen_random_uuid(), %s, %s, %s, %s, %s, 'in_progress', %s, %s)
                on conflict (user_id, key) do nothing
                returning id
                """,
                [
                    request.user_id,
                    request.key,
                    request.method,
                    request.path,
                    request.fingerprint,
                    now,
                    now + KEY_LIFETIME,
                ],
            ).fetchone()
            if claimed is not None:
                return Started()
            row = connection.execute(
                """
                select request_hash, state, response_status, response_body
                from private.idempotency_keys where user_id = %s and key = %s
                """,
                [request.user_id, request.key],
            ).fetchone()
        if row is None:  # pragma: no cover - the conflict means it exists
            return StillRunning()
        if bytes(row[0]) != request.fingerprint:
            return KeyReused()
        if row[1] != "completed":
            return StillRunning()
        return Replay(status=int(row[2]), body=row[3] or {})

    async def complete(
        self, request: IdempotentRequest, *, status: int, body: Mapping[str, Any]
    ) -> None:
        await self._execute(
            """
            update private.idempotency_keys
            set state = 'completed', response_status = %s, response_body = %s
            where user_id = %s and key = %s and state = 'in_progress'
            """,
            [status, Jsonb(dict(body)), request.user_id, request.key],
        )

    async def abandon(self, request: IdempotentRequest) -> None:
        await self._execute(
            """
            delete from private.idempotency_keys
            where user_id = %s and key = %s and state = 'in_progress'
            """,
            [request.user_id, request.key],
        )

    async def _execute(self, statement: str, params: list[object]) -> None:
        def run() -> None:
            with self._database.connection() as connection, connection.transaction():
                connection.execute(statement, params)

        await anyio.to_thread.run_sync(run)
