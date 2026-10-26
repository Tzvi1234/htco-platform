"""The order automations' Postgres side (slice 5.11): what verification compares and records,
and the per-listing sales counters, each change in one transaction, as the worker's role in its
job's organization."""

import logging
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
from htco_application.order_ingest import drift_id
from htco_application.shop_integrations import ShopIntegration
from htco_domain.orders import OrderState
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in

_SNAPSHOT = """
select etsy_updated_at,
       app.worker_order_grand_total_matches(shop_id, receipt_id, %s, %s, %s)
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

_STORED_RECEIPTS = """
select receipt_id from app.etsy_orders
where shop_id = %s and receipt_id > %s
order by receipt_id
limit %s
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

# The counter row first (so `for update` always has a row to lock: two recounts of a new listing
# never both read "none" and overwrite each other), then locked, then set.
_ENSURE_COUNTER = """
insert into app.product_sales_counters (organization_id, shop_id, listing_id, units, orders)
values (%s, %s, %s, 0, 0)
on conflict (shop_id, listing_id) do nothing
"""

_COUNTER = """
select units, orders from app.product_sales_counters
where shop_id = %s and listing_id = %s
for update
"""

_SET = """
update app.product_sales_counters set units = %s, orders = %s
where shop_id = %s and listing_id = %s
"""

_log = logging.getLogger(__name__)


class PostgresOrderAutomationStore:
    """`VerificationStore` and `CountersStore`."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def snapshot(self, shop_id: uuid.UUID, receipt: OrderReceipt) -> StoredSnapshot | None:
        total = receipt.grand_total
        receipt_id = receipt.receipt_id

        def run() -> StoredSnapshot | None:
            with self._database.connection() as c:
                row = c.execute(
                    _SNAPSHOT,
                    [total.amount, total.divisor, total.currency_code, shop_id, receipt_id],
                ).fetchone()
                if row is None:
                    return None
                items = c.execute(_ITEMS, [shop_id, receipt_id]).fetchall()
            return StoredSnapshot(
                etsy_updated_at=row[0],
                total_matches=row[1] is True,
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
                        [drift_id(integration.shop_id, receipt.receipt_id, difference.kind,
                                  receipt.updated_at),
                         integration.organization_id, integration.shop_id,
                         receipt.receipt_id, difference.kind, Jsonb(difference.stored),
                         Jsonb(difference.observed), receipt.updated_at, now, execution_id,
                         correlation_id[:200]],
                    ).rowcount  # fmt: skip
                    if inserted:
                        new += 1
                        add_outbox_in(
                            c,
                            difference_event(
                                integration, receipt, difference, correlation_id, detected_at=now
                            ),
                            now=now,
                        )
            return new

        return await anyio.to_thread.run_sync(run)

    async def stored_receipts(self, shop_id: uuid.UUID, *, after: int, limit: int) -> list[int]:
        def run() -> list[int]:
            with self._database.connection() as c:
                rows = c.execute(_STORED_RECEIPTS, [shop_id, after, limit]).fetchall()
            return [int(r[0]) for r in rows]

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
                    c.execute(_ENSURE_COUNTER, [organization_id, shop_id, listing])
                    current = c.execute(_COUNTER, [shop_id, listing]).fetchone()
                    if current is None:  # the row was just ensured: only RLS could hide it
                        raise LookupError("the listing's counter is not visible to this job")
                    units = int(current[0]) + delta.units.get(listing, 0)
                    orders = int(current[1]) + delta.orders.get(listing, 0)
                    if units < 0 or orders < 0:
                        # With the ledger this means drift or a bug: keep zero (the check
                        # constraint), and say so. IDs and numbers only.
                        _log.warning(
                            "product_sales.clamped: shop=%s listing=%s units=%s orders=%s",
                            shop_id, listing, units, orders,
                        )  # fmt: skip
                    c.execute(_SET, [max(units, 0), max(orders, 0), shop_id, listing])
                c.execute(
                    _KEEP,
                    [organization_id, shop_id, receipt_id,
                     Jsonb({str(k): v for k, v in desired.items()}), now, execution_id],
                )  # fmt: skip
            return delta

        return await anyio.to_thread.run_sync(run)
