"""The order automations' Postgres side (slice 5.11): what verification compares and records,
and the per-listing sales counters, each change in one transaction, as the worker's role in its
job's organization."""

import uuid
from datetime import datetime
from typing import Any

import anyio
from psycopg.types.json import Jsonb

from htco_application.marketplace import OrderReceipt
from htco_application.order_automations import (
    CounterDelta,
    Difference,
    StoredSnapshot,
    counter_delta,
    desired_counts,
    difference_event,
)
from htco_application.shop_integrations import ShopIntegration
from htco_domain.orders import OrderState
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in

_SNAPSHOT = """
select etsy_updated_at, grand_total_amount, grand_total_divisor, grand_total_currency
from app.etsy_orders
where shop_id = %s and receipt_id = %s
"""

_ITEMS = """
select transaction_id, quantity, listing_id
from app.etsy_order_items
where shop_id = %s and receipt_id = %s
order by position
"""

_INSERT_DRIFT = """
insert into app.sync_drifts (
  id, organization_id, shop_id, receipt_id, kind, stored, observed, etsy_updated_at, source,
  detected_at, execution_id, correlation_id
) values (%s, %s, %s, %s, %s, %s, %s, %s, 'verification', %s, %s, %s)
on conflict (shop_id, receipt_id, kind, etsy_updated_at) do nothing
"""

_ORDER_STATE = """
select order_state from app.etsy_orders where shop_id = %s and receipt_id = %s for update
"""

_PREVIOUS = """
select counted from app.product_sales_applications
where shop_id = %s and receipt_id = %s
for update
"""

_KEEP = """
insert into app.product_sales_applications
  (organization_id, shop_id, receipt_id, counted, applied_at, execution_id)
values (%s, %s, %s, %s, %s, %s)
on conflict (shop_id, receipt_id) do update
  set counted = excluded.counted, applied_at = excluded.applied_at,
      execution_id = excluded.execution_id
"""

_MOVE = """
insert into app.product_sales_counters (organization_id, shop_id, listing_id, units, orders)
values (%s, %s, %s, greatest(%s, 0), greatest(%s, 0))
on conflict (shop_id, listing_id) do update
  set units = greatest(app.product_sales_counters.units + %s, 0),
      orders = greatest(app.product_sales_counters.orders + %s, 0)
"""


class PostgresOrderAutomationStore:
    """`VerificationStore` and `CountersStore`."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def snapshot(self, shop_id: uuid.UUID, receipt_id: int) -> StoredSnapshot | None:
        def run() -> StoredSnapshot | None:
            with self._database.connection() as c:
                row = c.execute(_SNAPSHOT, [shop_id, receipt_id]).fetchone()
                if row is None:
                    return None
                items = c.execute(_ITEMS, [shop_id, receipt_id]).fetchall()
            return StoredSnapshot(
                etsy_updated_at=row[0],
                grand_total=(int(row[1]), int(row[2]), str(row[3])),
                items=tuple((int(r[0]), int(r[1])) for r in items),
            )

        return await anyio.to_thread.run_sync(run)

    async def record_differences(  # noqa: PLR0913 - the order, what differed and the run
        self,
        integration: ShopIntegration,
        receipt: OrderReceipt,
        differences: tuple[Difference, ...],
        *,
        execution_id: uuid.UUID,
        correlation_id: str,
        now: datetime,
    ) -> int:
        def run() -> int:
            new = 0
            with self._database.connection() as c, c.transaction():
                for difference in differences:
                    inserted = c.execute(
                        _INSERT_DRIFT,
                        [uuid.uuid7(), integration.organization_id, integration.shop_id,
                         receipt.receipt_id, difference.kind, Jsonb(difference.stored),
                         Jsonb(difference.observed), receipt.updated_at, now, execution_id,
                         correlation_id[:200]],
                    ).rowcount  # fmt: skip
                    if inserted:
                        new += 1
                        add_outbox_in(
                            c,
                            difference_event(integration, receipt, difference, correlation_id),
                            now=now,
                        )
            return new

        return await anyio.to_thread.run_sync(run)

    async def recount(
        self,
        organization_id: uuid.UUID,
        shop_id: uuid.UUID,
        receipt_id: int,
        *,
        execution_id: uuid.UUID,
        now: datetime,
    ) -> CounterDelta | None:
        def run() -> CounterDelta | None:
            with self._database.connection() as c, c.transaction():
                row = c.execute(_ORDER_STATE, [shop_id, receipt_id]).fetchone()
                if row is None:
                    return None
                state = None if row[0] is None else OrderState(row[0])
                items = c.execute(_ITEMS, [shop_id, receipt_id]).fetchall()
                desired = desired_counts(
                    state, tuple((None if r[2] is None else int(r[2]), int(r[1])) for r in items)
                )
                previous_row = c.execute(_PREVIOUS, [shop_id, receipt_id]).fetchone()
                previous: dict[int, int] = (
                    {} if previous_row is None
                    else {int(k): int(v) for k, v in dict[str, Any](previous_row[0]).items()}
                )  # fmt: skip
                delta = counter_delta(previous, desired)
                for listing in sorted(set(delta.units) | set(delta.orders)):
                    units = delta.units.get(listing, 0)
                    orders = delta.orders.get(listing, 0)
                    c.execute(_MOVE, [organization_id, shop_id, listing, units, orders, units,
                                      orders])  # fmt: skip
                c.execute(
                    _KEEP,
                    [organization_id, shop_id, receipt_id,
                     Jsonb({str(k): v for k, v in desired.items()}), now, execution_id],
                )  # fmt: skip
            return delta

        return await anyio.to_thread.run_sync(run)
