"""The web session store (slice 3.5): sealed Supabase tokens in `private.web_sessions`.

Runs with the API's login role: no user is signed in when a session is created or resumed, and
no client role can reach these tables. Tokens are sealed with the session ID as context, so a
sealed token moved to another row does not open.
"""

import uuid
from datetime import datetime
from typing import Any

import anyio

from htco_application.web_sessions import IssuedTokens, NewSession, StoredSession
from htco_persistence.database import Database
from htco_platform.crypto import Sealer

_COLUMNS = """
id, token_hash, user_id, access_token, refresh_token, access_expires_at, created_at,
last_seen_at, idle_expires_at, absolute_expires_at, revoked_at, user_agent, reauthenticated_at
"""


class PostgresSessionStore:
    def __init__(self, database: Database, sealer: Sealer) -> None:
        self._database = database
        self._sealer = sealer

    async def create(self, session: NewSession) -> None:
        await anyio.to_thread.run_sync(self._create, session)

    async def find(self, token_hash: bytes) -> StoredSession | None:
        return await anyio.to_thread.run_sync(self._find, token_hash)

    async def replace_tokens(self, session_id: uuid.UUID, issued: IssuedTokens) -> None:
        await anyio.to_thread.run_sync(self._replace_tokens, session_id, issued)

    async def touch(self, session_id: uuid.UUID, at: datetime, idle_expires_at: datetime) -> None:
        await self._execute(
            "update private.web_sessions set last_seen_at = %s, idle_expires_at = %s where id = %s",
            [at, idle_expires_at, session_id],
        )

    async def revoke(self, session_id: uuid.UUID, at: datetime) -> None:
        await self._execute(
            """
            update private.web_sessions set revoked_at = %s
            where id = %s and revoked_at is null
            """,
            [at, session_id],
        )

    async def set_reauthenticated(self, session_id: uuid.UUID, at: datetime) -> None:
        await self._execute(
            "update private.web_sessions set reauthenticated_at = %s where id = %s",
            [at, session_id],
        )

    async def get(self, session_id: uuid.UUID) -> StoredSession | None:
        return await anyio.to_thread.run_sync(self._select_one, "id", session_id)

    async def list_active(self, user_id: uuid.UUID, now: datetime) -> list[StoredSession]:
        def run() -> list[StoredSession]:
            with self._database.connection() as connection:
                rows = connection.execute(
                    f"""
                    select {_COLUMNS} from private.web_sessions
                    where user_id = %(user)s and revoked_at is null
                      and idle_expires_at > %(now)s and absolute_expires_at > %(now)s
                    order by last_seen_at desc
                    """,  # noqa: S608 - constant column list
                    {"user": user_id, "now": now},
                ).fetchall()
            return [self._row(row) for row in rows]

        return await anyio.to_thread.run_sync(run)

    async def record_attempt(
        self, email_hash: bytes, ip_hash: bytes | None, *, succeeded: bool, at: datetime
    ) -> None:
        await self._execute(
            """
            insert into private.sign_in_attempts (email_hash, ip_hash, succeeded, attempted_at)
            values (%s, %s, %s, %s)
            """,
            [email_hash, ip_hash, succeeded, at],
        )

    async def recent_failures(
        self, email_hash: bytes, ip_hash: bytes | None, since: datetime
    ) -> tuple[int, int]:
        return await anyio.to_thread.run_sync(self._recent_failures, email_hash, ip_hash, since)

    async def succeeded_since(self, key: bytes, since: datetime) -> bool:
        def run() -> bool:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    select exists (
                      select 1 from private.sign_in_attempts
                      where email_hash = %s and succeeded and attempted_at >= %s
                    )
                    """,
                    [key, since],
                ).fetchone()
            return row is not None and bool(row[0])

        return await anyio.to_thread.run_sync(run)

    # Sync work in a worker thread ----------------------------------------------------------------

    def _seal(self, session_id: uuid.UUID, issued: IssuedTokens) -> tuple[bytes, bytes]:
        context = session_id.bytes
        return (
            self._sealer.seal(issued.access_token, context=context),
            self._sealer.seal(issued.refresh_token, context=context),
        )

    def _create(self, session: NewSession) -> None:
        access, refresh = self._seal(session.id, session.tokens)
        with self._database.connection() as connection:
            connection.execute(
                """
                insert into private.web_sessions
                  (id, token_hash, user_id, access_token, refresh_token, access_expires_at,
                   created_at, last_seen_at, idle_expires_at, absolute_expires_at, user_agent)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    session.id,
                    session.token_hash,
                    session.user_id,
                    access,
                    refresh,
                    session.tokens.expires_at,
                    session.created_at,
                    session.created_at,
                    session.idle_expires_at,
                    session.absolute_expires_at,
                    session.user_agent,
                ],
            )

    def _find(self, token_hash: bytes) -> StoredSession | None:
        return self._select_one("token_hash", token_hash)

    def _select_one(self, column: str, value: object) -> StoredSession | None:
        if column not in {"id", "token_hash"}:  # pragma: no cover - internal callers only
            raise ValueError(column)
        with self._database.connection() as connection:
            row = connection.execute(
                f"select {_COLUMNS} from private.web_sessions where {column} = %s",  # noqa: S608 - constant identifiers
                [value],
            ).fetchone()
        return None if row is None else self._row(row)

    def _row(self, row: tuple[Any, ...]) -> StoredSession:
        session_id: uuid.UUID = row[0]
        context = session_id.bytes
        return StoredSession(
            id=session_id,
            token_hash=bytes(row[1]),
            user_id=row[2],
            tokens=IssuedTokens(
                access_token=self._sealer.open(bytes(row[3]), context=context),
                refresh_token=self._sealer.open(bytes(row[4]), context=context),
                expires_at=row[5],
            ),
            created_at=row[6],
            last_seen_at=row[7],
            idle_expires_at=row[8],
            absolute_expires_at=row[9],
            revoked_at=row[10],
            user_agent=row[11],
            reauthenticated_at=row[12],
        )

    def _replace_tokens(self, session_id: uuid.UUID, issued: IssuedTokens) -> None:
        access, refresh = self._seal(session_id, issued)
        with self._database.connection() as connection:
            connection.execute(
                """
                update private.web_sessions
                set access_token = %s, refresh_token = %s, access_expires_at = %s
                where id = %s
                """,
                [access, refresh, issued.expires_at, session_id],
            )

    def _recent_failures(
        self, email_hash: bytes, ip_hash: bytes | None, since: datetime
    ) -> tuple[int, int]:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select
                  count(*) filter (where email_hash = %(email)s),
                  count(*) filter (where %(ip)s::bytea is not null and ip_hash = %(ip)s::bytea)
                from private.sign_in_attempts
                where not succeeded and attempted_at >= %(since)s
                  and (email_hash = %(email)s or ip_hash = %(ip)s::bytea)
                """,
                {"email": email_hash, "ip": ip_hash, "since": since},
            ).fetchone()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    async def _execute(self, sql: str, params: list[object]) -> None:
        def run() -> None:
            with self._database.connection() as connection:
                connection.execute(sql, params)

        await anyio.to_thread.run_sync(run)
