"""The database readiness probe behind `/readyz` (AC-2.1.3, ER-OPS-01)."""

from dataclasses import dataclass, field

import anyio.to_thread
import psycopg


@dataclass(frozen=True)
class DatabaseReadinessCheck:
    """Opens a short-lived connection and runs `select 1`; raises when the database is unusable.

    A fresh connection per probe keeps the check honest (a pooled connection could hide a
    database that stopped accepting new ones) and needs no state, so the API recovers the moment
    the database is back. The probe runs the synchronous driver in a worker thread: it works on
    every event loop, including the Windows one used in development. The connection string holds
    a password, so it is kept out of `repr`.
    """

    conninfo: str = field(repr=False)
    name: str = "database"
    connect_timeout_seconds: int = 1

    async def __call__(self) -> None:
        await anyio.to_thread.run_sync(self._probe, abandon_on_cancel=True)

    def _probe(self) -> None:
        with psycopg.connect(
            self.conninfo,
            connect_timeout=self.connect_timeout_seconds,
            autocommit=True,
            application_name="htco-readiness",
        ) as connection:
            connection.execute("select 1")
