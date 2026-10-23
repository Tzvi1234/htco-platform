"""Database connections, user-scoped transactions for RLS (slice 2.3, ADR-0011), and the worker's
own role with one organization per job (slice 5.1).

A user-scoped transaction runs as the `authenticated` role with the verified token's claims in
`request.jwt.claims`, the way Supabase's own API does, so `auth.uid()` and the row-level security
policies of slice 2.2 apply to every statement. Both are set with `set local`: they end with the
transaction, and the pooled connection goes back to its login role with no claims. A query that
forgets a tenant filter therefore still returns only the user's rows (defense in depth).

The worker's pool (`role="htco_worker"`) switches every connection to that role when it opens,
and names the organization of the job being run (`tenant_scope`) in the session setting
`htco.organization_id` each time a connection is taken from the pool. The worker role's policies
compare every tenant row with it, so a job reaches its own organization's rows only, and a job
without an organization reaches none.

The pool is synchronous and work runs in worker threads, like the readiness check: psycopg's
async driver cannot run on Windows' default event loop. The tenant scope is a context variable,
which `anyio.to_thread.run_sync` carries into those threads.
"""

import contextvars
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, override

import anyio
import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TENANT: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "htco_tenant", default=None
)


@contextmanager
def tenant_scope(organization_id: uuid.UUID | None) -> Iterator[None]:
    """Run the enclosed work for one organization (or none) on role-switched connections."""
    token = _TENANT.set(organization_id)
    try:
        yield
    finally:
        _TENANT.reset(token)


class Database:
    def __init__(
        self,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        timeout: float = 5.0,
        role: str | None = None,
    ) -> None:
        self._conninfo = conninfo
        if role is not None and not _ROLE.match(role):
            raise ValueError("invalid database role name")
        self._role = role
        self._pool = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
            name="htco" if role is None else f"htco-{role}",
            configure=None if role is None else self._switch_role,
        )

    def _switch_role(self, connection: psycopg.Connection[Any]) -> None:
        connection.execute(sql.SQL("set role {}").format(sql.Identifier(str(self._role))))
        connection.commit()

    async def start(self) -> None:
        await anyio.to_thread.run_sync(self._pool.open)

    async def stop(self) -> None:
        await anyio.to_thread.run_sync(self._pool.close)

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """A pooled connection with the pool's role. Not for user requests. On a role-switched
        pool it carries the current `tenant_scope`."""
        with self._pool.connection() as connection:
            if self._role is not None:
                tenant = _TENANT.get()
                connection.execute(
                    "select set_config('htco.organization_id', %s, false)",
                    ["" if tenant is None else str(tenant)],
                )
                connection.commit()
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
