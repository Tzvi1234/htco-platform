"""The database readiness check for `/readyz` (slice 2.1, AC-2.1.3)."""

from typing import override

import anyio
import psycopg


class DatabaseCheck:
    """Ready when a new connection to the database answers `select 1`.

    A fresh connection per probe (not a pool) tests what matters for readiness: that the database
    accepts connections now. When it comes back after an outage, the next probe sees it, with no
    restart. The probe runs a synchronous connection in a worker thread, so it works on every event
    loop (psycopg's async driver cannot use Windows' default loop); `connect_timeout` bounds it.
    The connection string is a secret and never appears in `repr` or logs.
    """

    name = "database"

    def __init__(self, conninfo: str, *, connect_timeout: int = 1) -> None:
        self._conninfo = conninfo
        self._connect_timeout = connect_timeout

    async def __call__(self) -> None:
        await anyio.to_thread.run_sync(self._probe)

    def _probe(self) -> None:
        with psycopg.connect(
            self._conninfo, connect_timeout=self._connect_timeout, autocommit=True
        ) as connection:
            connection.execute("select 1")

    @override
    def __repr__(self) -> str:
        return "DatabaseCheck(<conninfo hidden>)"
