"""Scheduled maintenance on Postgres (slice 5.1): the shop lists of the daily fan-out and the
purge of a disconnected shop's data.

Both run on the worker's pool (`htco_worker`). The lists come from two security-definer functions
that return IDs only; the purge runs in the shop's organization (`tenant_scope`) and still names
the organization and the shop in every statement (explicit tenant filters on top of RLS).
"""

from dataclasses import replace
from datetime import datetime

import anyio

from htco_application.audit import AuditRecord
from htco_application.kernel import TenantShop
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

# The shop's synced data, table by table: the probe results and the stored order history (the
# order's items, refunds and buyer row cascade from it; post-merge review of 2026-10-23). Legal
# retention keeps nothing of it yet (DATA_MODEL §6).
_PURGED_TABLES = ("app.shop_capabilities", "app.etsy_orders", "app.etsy_order_sync_state")


class PostgresShopDirectory:
    def __init__(self, database: Database) -> None:
        self._database = database

    def connected_shops(self) -> list[TenantShop]:
        return self._shops("select organization_id, shop_id from jobs.connected_shops()", [])

    def purgeable_shops(self, *, now: datetime) -> list[TenantShop]:
        return self._shops("select organization_id, shop_id from jobs.purgeable_shops(%s)", [now])

    def _shops(self, query: str, params: list[object]) -> list[TenantShop]:
        with self._database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [TenantShop(organization_id=row[0], shop_id=row[1]) for row in rows]


class PostgresPurgeStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def purge(
        self, shop: TenantShop, *, now: datetime, audit: AuditRecord
    ) -> dict[str, int] | None:
        return await anyio.to_thread.run_sync(self._purge, shop, now, audit)

    def _purge(self, shop: TenantShop, now: datetime, audit: AuditRecord) -> dict[str, int] | None:
        with self._database.connection() as connection, connection.transaction():
            # Locked like a reconnect locks it: a reconnect either happened before (nothing is
            # due any more) or waits until the purge committed.
            due = connection.execute(
                """
                select 1 from app.shop_integrations
                where organization_id = %s and shop_id = %s and provider = 'etsy'
                  and status = 'disconnected' and purge_after <= %s
                for update
                """,
                [shop.organization_id, shop.shop_id, now],
            ).fetchone()
            if due is None:
                return None
            deleted: dict[str, int] = {}
            for table in _PURGED_TABLES:
                # The row count, not `returning`: the worker reads no column it does not need.
                deleted[table.removeprefix("app.")] = connection.execute(
                    f"delete from {table} where organization_id = %s and shop_id = %s",  # noqa: S608 - constant table names
                    [shop.organization_id, shop.shop_id],
                ).rowcount
            # Purged: no date left to wait for (the disconnect date stays, for the record).
            connection.execute(
                """
                update app.shop_integrations set purge_after = null
                where organization_id = %s and shop_id = %s and provider = 'etsy'
                """,
                [shop.organization_id, shop.shop_id],
            )
            append_audit(connection, replace(audit, after={"deleted": deleted}))
        return deleted
