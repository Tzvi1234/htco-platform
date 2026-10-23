"""Shop repositories (slice 2.4 part 2) and removing a shop with everything stored about it."""

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import anyio

from htco_application.audit import AuditRecord
from htco_application.shop_integrations import RemovableShop, RemovedShop
from htco_application.shops import ShopQuery, ShopSummary
from htco_domain.integrations import IntegrationStatus
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

# Runs as the caller (RLS), with the application's scope as explicit filters on top.
_LIST_SHOPS = """
select id, organization_id, name, marketplace, status
from app.shops
where organization_id = %(organization_id)s
  and (%(only_shops)s::uuid[] is null or id = any(%(only_shops)s::uuid[]))
  and (%(shop_id)s::uuid is null or id = %(shop_id)s::uuid)
  and (%(after)s::uuid is null or id > %(after)s::uuid)
order by id
limit %(limit)s
"""


class PostgresShopReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_shops(
        self, claims: Mapping[str, Any], query: ShopQuery
    ) -> Sequence[ShopSummary]:
        return await anyio.to_thread.run_sync(self._list, claims, query)

    def _list(self, claims: Mapping[str, Any], query: ShopQuery) -> list[ShopSummary]:
        parameters = {
            "organization_id": query.organization_id,
            "only_shops": None if query.only_shops is None else sorted(query.only_shops),
            "shop_id": query.shop_id,
            "after": query.after,
            "limit": query.limit,
        }
        with self._database.user_transaction(claims) as connection:
            rows = connection.execute(_LIST_SHOPS, parameters).fetchall()
        return [
            ShopSummary(
                id=row[0], organization_id=row[1], name=row[2], marketplace=row[3], status=row[4]
            )
            for row in rows
        ]


class PostgresShopRemover:
    """`ShopRemover` with the API's login role: one transaction, the integration row locked
    first (like disconnect and reconnect lock it), then the shop."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find(self, shop_id: uuid.UUID) -> RemovableShop | None:
        def run() -> RemovableShop | None:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    select s.id, s.organization_id, s.name, s.status, i.status
                    from app.shops s
                    left join app.shop_integrations i on i.shop_id = s.id
                    where s.id = %s
                    """,
                    [shop_id],
                ).fetchone()
            if row is None:
                return None
            return RemovableShop(
                id=row[0],
                organization_id=row[1],
                name=row[2],
                status=row[3],
                integration_status=None if row[4] is None else IntegrationStatus(row[4]),
            )

        return await anyio.to_thread.run_sync(run)

    async def remove(self, shop: RemovableShop, *, audit: AuditRecord) -> RemovedShop | None:
        def run() -> RemovedShop | None:
            ids = [shop.id, shop.organization_id]
            with self._database.connection() as connection, connection.transaction():
                connection.execute(
                    """
                    select 1 from app.shop_integrations
                    where shop_id = %s and organization_id = %s for update
                    """,
                    ids,
                )
                owned = connection.execute(
                    "select 1 from app.shops where id = %s and organization_id = %s for update",
                    ids,
                ).fetchone()
                if owned is None:
                    return None
                # The connection first: its tokens are gone before anything else.
                connection.execute(
                    "delete from app.shop_integrations where shop_id = %s", [shop.id]
                )
                connection.execute(
                    "delete from app.shop_capabilities where shop_id = %s", [shop.id]
                )
                connection.execute(
                    "delete from app.etsy_order_sync_state where shop_id = %s", [shop.id]
                )
                # Items, refunds and buyer rows cascade from their order.
                orders = connection.execute(
                    "delete from app.etsy_orders where shop_id = %s", [shop.id]
                ).rowcount
                connection.execute("delete from app.shop_access where shop_id = %s", [shop.id])
                connection.execute(
                    "delete from app.shops where id = %s and organization_id = %s", ids
                )
                after = {**(audit.after or {}), "orders_deleted": orders}
                append_audit(connection, dataclasses.replace(audit, after=after))
            return RemovedShop(shop_id=shop.id, orders_deleted=orders)

        return await anyio.to_thread.run_sync(run)
