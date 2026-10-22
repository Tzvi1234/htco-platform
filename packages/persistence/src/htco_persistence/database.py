"""The API's database connections, and user-scoped transactions for RLS (slice 2.3, ADR-0011).

A user-scoped transaction runs as the `authenticated` role with the verified token's claims in
`request.jwt.claims`, the way Supabase's own API does, so `auth.uid()` and the row-level security
policies of slice 2.2 apply to every statement. Both are set with `set local`: they end with the
transaction, and the pooled connection goes back to its login role with no claims. A query that
forgets a tenant filter therefore still returns only the user's rows (defense in depth).

The pool is synchronous and work runs in worker threads, like the readiness check: psycopg's
async driver cannot run on Windows' default event loop.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, override

import anyio
import psycopg
from psycopg_pool import ConnectionPool


class Database:
    def __init__(
        self, conninfo: str, *, min_size: int = 1, max_size: int = 4, timeout: float = 5.0
    ) -> None:
        self._conninfo = conninfo
        self._pool = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
            name="htco",
        )

    async def start(self) -> None:
        await anyio.to_thread.run_sync(self._pool.open)

    async def stop(self) -> None:
        await anyio.to_thread.run_sync(self._pool.close)

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """A pooled connection with the login role's privileges. Not for user requests."""
        with self._pool.connection() as connection:
            yield connection

    def dedicated_connection(self) -> psycopg.Connection[Any]:
        """A new connection outside the pool (autocommit), for a session-level lock held for
        minutes; the caller closes it."""
        return psycopg.connect(self._conninfo, autocommit=True, connect_timeout=5)

    @contextmanager
    def user_transaction(self, claims: Mapping[str, Any]) -> Iterator[psycopg.Connection[Any]]:
        """One transaction as the signed-in user: committed on success, rolled back on error."""
        with self._pool.connection() as connection, connection.transaction():
            connection.execute("set local role authenticated")
            connection.execute(
                "select set_config('request.jwt.claims', %s, true)",
                [json.dumps(dict(claims), default=str)],
            )
            yield connection

    @override
    def __repr__(self) -> str:
        return "Database(<conninfo hidden>)"
