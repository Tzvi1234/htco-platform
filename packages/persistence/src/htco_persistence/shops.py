"""Shop repositories (slice 2.4 part 2)."""

from collections.abc import Mapping, Sequence
from typing import Any

import anyio

from htco_application.shops import ShopQuery, ShopSummary
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
