"""The shop's stored order history in Postgres (`app.etsy_orders` and its tables).

Runs with the API's login role; the application authorizes first and asks for the buyer table
only for roles that may see buyer data. Upserts are idempotent by `(shop_id, receipt_id)`: a
replay or an older copy of a receipt changes nothing, a newer one replaces the order with its
items, refunds and buyer row. Nothing here logs; database errors are raised as they are and the
caller logs their type only (a message can quote a row).
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from htco_application.marketplace import (
    MarketplaceMoney,
    OrderLine,
    OrderReceipt,
    OrderRefund,
    OrderShipment,
    ShippingAddress,
    display_name,
)
from htco_application.order_history import SyncState
from htco_application.shop_integrations import ShopIntegration
from htco_persistence.database import Database

_STATE = """
select s.shop_id, s.etsy_shop_id, s.backfill_offset, s.backfill_completed_at,
       s.incremental_synced_at, s.last_error,
       (select count(*) from app.etsy_orders o
         where o.shop_id = s.shop_id and o.etsy_shop_id = s.etsy_shop_id)
from app.etsy_order_sync_state s
where s.shop_id = %s
"""

# A different Etsy shop behind the HTCO shop starts the backfill over.
_ENSURE_STATE = """
insert into app.etsy_order_sync_state (shop_id, organization_id, etsy_shop_id, last_attempt_at)
values (%s, %s, %s, %s)
on conflict (shop_id) do update set
  last_attempt_at = excluded.last_attempt_at,
  backfill_offset = case when app.etsy_order_sync_state.etsy_shop_id = excluded.etsy_shop_id
                         then app.etsy_order_sync_state.backfill_offset else 0 end,
  backfill_completed_at = case when app.etsy_order_sync_state.etsy_shop_id = excluded.etsy_shop_id
                         then app.etsy_order_sync_state.backfill_completed_at end,
  incremental_synced_at = case when app.etsy_order_sync_state.etsy_shop_id = excluded.etsy_shop_id
                         then app.etsy_order_sync_state.incremental_synced_at end,
  etsy_shop_id = excluded.etsy_shop_id
"""

_UPSERT_ORDER = """
insert into app.etsy_orders as o (
  organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
  grand_total_amount, grand_total_divisor, grand_total_currency,
  subtotal_amount, subtotal_divisor, subtotal_currency,
  shipping_amount, shipping_divisor, shipping_currency,
  discount_amount, discount_divisor, discount_currency,
  country_code, shipments, etsy_created_at, etsy_updated_at, synced_at
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s)
on conflict (shop_id, receipt_id) do update set
  etsy_shop_id = excluded.etsy_shop_id, status = excluded.status, is_paid = excluded.is_paid,
  is_shipped = excluded.is_shipped,
  grand_total_amount = excluded.grand_total_amount,
  grand_total_divisor = excluded.grand_total_divisor,
  grand_total_currency = excluded.grand_total_currency,
  subtotal_amount = excluded.subtotal_amount, subtotal_divisor = excluded.subtotal_divisor,
  subtotal_currency = excluded.subtotal_currency,
  shipping_amount = excluded.shipping_amount, shipping_divisor = excluded.shipping_divisor,
  shipping_currency = excluded.shipping_currency,
  discount_amount = excluded.discount_amount, discount_divisor = excluded.discount_divisor,
  discount_currency = excluded.discount_currency,
  country_code = excluded.country_code, shipments = excluded.shipments,
  etsy_created_at = excluded.etsy_created_at, etsy_updated_at = excluded.etsy_updated_at,
  synced_at = excluded.synced_at
where o.etsy_updated_at <= excluded.etsy_updated_at
returning o.receipt_id
"""

_INSERT_ITEM = """
insert into app.etsy_order_items (
  organization_id, shop_id, receipt_id, transaction_id, position, listing_id, title, sku,
  quantity, unit_price_amount, unit_price_divisor, unit_price_currency, is_digital,
  expected_ship_date
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_REFUND = """
insert into app.etsy_order_refunds (
  organization_id, shop_id, receipt_id, position, amount, divisor, currency, refunded_at,
  reason, status, note_from_issuer
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_BUYER = """
insert into app.etsy_order_buyers (
  organization_id, shop_id, receipt_id, buyer_name, address_first_line, address_second_line,
  address_city, address_state, address_postal_code, address_country_code, shipments
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_ORDERS = """
select o.receipt_id, o.status, o.is_paid, o.is_shipped,
       o.grand_total_amount, o.grand_total_divisor, o.grand_total_currency,
       o.subtotal_amount, o.subtotal_divisor, o.subtotal_currency,
       o.shipping_amount, o.shipping_divisor, o.shipping_currency,
       o.discount_amount, o.discount_divisor, o.discount_currency,
       o.country_code, o.shipments, o.etsy_created_at, o.etsy_updated_at
from app.etsy_orders o
where o.shop_id = %s and o.etsy_shop_id = %s
  and (%s::timestamptz is null or o.etsy_created_at >= %s::timestamptz)
order by o.etsy_created_at desc, o.receipt_id desc
"""

_ITEMS = """
select i.receipt_id, i.transaction_id, i.listing_id, i.title, i.sku, i.quantity,
       i.unit_price_amount, i.unit_price_divisor, i.unit_price_currency, i.is_digital,
       i.expected_ship_date
from app.etsy_order_items i
join app.etsy_orders o on o.shop_id = i.shop_id and o.receipt_id = i.receipt_id
where i.shop_id = %s and o.etsy_shop_id = %s
  and (%s::timestamptz is null or o.etsy_created_at >= %s::timestamptz)
order by i.receipt_id, i.position
"""

_REFUNDS = """
select r.receipt_id, r.amount, r.divisor, r.currency, r.refunded_at, r.reason, r.status,
       r.note_from_issuer
from app.etsy_order_refunds r
join app.etsy_orders o on o.shop_id = r.shop_id and o.receipt_id = r.receipt_id
where r.shop_id = %s and o.etsy_shop_id = %s
  and (%s::timestamptz is null or o.etsy_created_at >= %s::timestamptz)
order by r.receipt_id, r.position
"""

_BUYERS = """
select b.receipt_id, b.buyer_name, b.address_first_line, b.address_second_line, b.address_city,
       b.address_state, b.address_postal_code, b.address_country_code, b.shipments
from app.etsy_order_buyers b
join app.etsy_orders o on o.shop_id = b.shop_id and o.receipt_id = b.receipt_id
where b.shop_id = %s and o.etsy_shop_id = %s
  and (%s::timestamptz is null or o.etsy_created_at >= %s::timestamptz)
"""


def _money(amount: int, divisor: int, currency: str) -> MarketplaceMoney:
    return MarketplaceMoney(amount=amount, divisor=divisor, currency_code=currency)


def _shipments(receipt: OrderReceipt, *, tracking: bool) -> Jsonb:
    return Jsonb(
        [
            {"carrier": s.carrier, "notified_at": s.notified_at.isoformat()}
            | ({"tracking_code": s.tracking_code} if tracking else {})
            for s in receipt.shipments
        ]
    )


def _upsert(
    connection: psycopg.Connection[Any],
    integration: ShopIntegration,
    receipts: Sequence[OrderReceipt],
    now: datetime,
) -> None:
    organization, shop = integration.organization_id, integration.shop_id
    for r in receipts:
        changed = connection.execute(
            _UPSERT_ORDER,
            [
                organization,
                shop,
                integration.etsy_shop_id,
                r.receipt_id,
                r.status,
                r.is_paid,
                r.is_shipped,
                *_triple(r.grand_total),
                *_triple(r.subtotal),
                *_triple(r.shipping_total),
                *_triple(r.discount_total),
                r.country_code,
                _shipments(r, tracking=False),
                r.created_at,
                r.updated_at,
                now,
            ],
        ).fetchone()
        if changed is None:  # an older copy than the stored one: nothing changes
            continue
        key = [shop, r.receipt_id]
        for table in ("etsy_order_items", "etsy_order_refunds", "etsy_order_buyers"):
            connection.execute(
                f"delete from app.{table} where shop_id = %s and receipt_id = %s",  # noqa: S608 - fixed table names
                key,
            )
        for position, line in enumerate(r.lines):
            connection.execute(
                _INSERT_ITEM,
                [
                    organization,
                    shop,
                    r.receipt_id,
                    line.transaction_id,
                    position,
                    line.listing_id,
                    line.title,
                    line.sku,
                    line.quantity,
                    *_triple(line.unit_price),
                    line.is_digital,
                    line.expected_ship_date,
                ],
            )
        for position, refund in enumerate(r.refunds):
            connection.execute(
                _INSERT_REFUND,
                [
                    organization,
                    shop,
                    r.receipt_id,
                    position,
                    *_triple(refund.amount),
                    refund.created_at,
                    refund.reason,
                    refund.status,
                    refund.note_from_issuer,
                ],
            )
        address = r.shipping_address
        connection.execute(
            _INSERT_BUYER,
            [
                organization,
                shop,
                r.receipt_id,
                r.buyer_name,
                None if address is None else address.first_line,
                None if address is None else address.second_line,
                None if address is None else address.city,
                None if address is None else address.state,
                None if address is None else address.postal_code,
                None if address is None else address.country_code,
                _shipments(r, tracking=True),
            ],
        )


def _triple(money: MarketplaceMoney) -> tuple[int, int, str]:
    return money.amount, money.divisor, money.currency_code


class PostgresOrderStore:
    """The `OrderStore` port."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def state(self, shop_id: uuid.UUID) -> SyncState | None:
        def run() -> SyncState | None:
            with self._database.connection() as connection:
                row = connection.execute(_STATE, [shop_id]).fetchone()
            if row is None:
                return None
            return SyncState(
                shop_id=row[0],
                etsy_shop_id=row[1],
                backfill_offset=row[2],
                backfill_completed_at=row[3],
                incremental_synced_at=row[4],
                last_error=row[5],
                orders_stored=row[6],
            )

        return await anyio.to_thread.run_sync(run)

    async def save_backfill_page(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        next_offset: int | None,
        now: datetime,
    ) -> None:
        def run() -> None:
            with self._database.connection() as connection, connection.transaction():
                connection.execute(
                    _ENSURE_STATE,
                    [
                        integration.shop_id,
                        integration.organization_id,
                        integration.etsy_shop_id,
                        now,
                    ],
                )
                _upsert(connection, integration, receipts, now)
                connection.execute(
                    """
                    update app.etsy_order_sync_state
                    set backfill_offset = coalesce(%s, backfill_offset),
                        backfill_completed_at = case when %s then %s else backfill_completed_at end,
                        last_error = null
                    where shop_id = %s
                    """,
                    [next_offset, next_offset is None, now, integration.shop_id],
                )

        await anyio.to_thread.run_sync(run)

    async def save_incremental_page(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        finished_at: datetime | None,
        now: datetime,
    ) -> None:
        def run() -> None:
            with self._database.connection() as connection, connection.transaction():
                connection.execute(
                    _ENSURE_STATE,
                    [
                        integration.shop_id,
                        integration.organization_id,
                        integration.etsy_shop_id,
                        now,
                    ],
                )
                _upsert(connection, integration, receipts, now)
                if finished_at is not None:
                    connection.execute(
                        """
                        update app.etsy_order_sync_state
                        set incremental_synced_at = %s, last_error = null
                        where shop_id = %s
                        """,
                        [finished_at, integration.shop_id],
                    )

        await anyio.to_thread.run_sync(run)

    async def record_failure(self, shop_id: uuid.UUID, *, error: str, now: datetime) -> None:
        def run() -> None:
            with self._database.connection() as connection, connection.transaction():
                connection.execute(
                    """
                    update app.etsy_order_sync_state set last_error = %s, last_attempt_at = %s
                    where shop_id = %s
                    """,
                    [error[:64], now, shop_id],
                )

        await anyio.to_thread.run_sync(run)

    async def load(
        self,
        shop_id: uuid.UUID,
        etsy_shop_id: int,
        *,
        since: datetime | None,
        buyer_data: bool,
    ) -> tuple[OrderReceipt, ...]:
        def run() -> tuple[OrderReceipt, ...]:
            window = [shop_id, etsy_shop_id, since, since]
            with self._database.connection() as connection:
                orders = connection.execute(_ORDERS, window).fetchall()
                items = connection.execute(_ITEMS, window).fetchall()
                refunds = connection.execute(_REFUNDS, window).fetchall()
                buyers = connection.execute(_BUYERS, window).fetchall() if buyer_data else []
            return _receipts(orders, items, refunds, buyers)

        return await anyio.to_thread.run_sync(run)

    @asynccontextmanager
    async def lock(self, shop_id: uuid.UUID) -> AsyncIterator[bool]:
        """A session-level advisory lock on a connection of its own (outside the pool, so a long
        backfill never holds a pooled connection). Postgres releases it if the process dies."""
        key = f"htco.etsy_order_sync:{shop_id}"
        connection = await anyio.to_thread.run_sync(self._database.dedicated_connection)
        try:
            row = await anyio.to_thread.run_sync(
                lambda: connection.execute(
                    "select pg_try_advisory_lock(hashtextextended(%s, 0))", [key]
                ).fetchone()
            )
            held = bool(row and row[0])
            yield held
        finally:
            await anyio.to_thread.run_sync(connection.close)

    async def purge_disconnected(self, *, now: datetime) -> int:
        """Delete the stored orders of shops disconnected past their `purge_after` (the Phase 5
        worker runs it on a schedule). Returns the number of orders deleted."""

        def run() -> int:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    "select private.purge_disconnected_etsy_orders(%s)", [now]
                ).fetchone()
            return int(row[0]) if row else 0

        return await anyio.to_thread.run_sync(run)


def _receipts(
    orders: list[tuple[Any, ...]],
    items: list[tuple[Any, ...]],
    refunds: list[tuple[Any, ...]],
    buyers: list[tuple[Any, ...]],
) -> tuple[OrderReceipt, ...]:
    lines: dict[int, list[OrderLine]] = {}
    for row in items:
        lines.setdefault(row[0], []).append(
            OrderLine(
                transaction_id=row[1],
                listing_id=row[2],
                title=row[3],
                sku=row[4],
                quantity=row[5],
                unit_price=_money(row[6], row[7], row[8]),
                is_digital=row[9],
                expected_ship_date=row[10],
            )
        )
    refunded: dict[int, list[OrderRefund]] = {}
    for row in refunds:
        refunded.setdefault(row[0], []).append(
            OrderRefund(
                amount=_money(row[1], row[2], row[3]),
                created_at=row[4],
                reason=row[5],
                status=row[6],
                note_from_issuer=row[7],
            )
        )
    who = {row[0]: row for row in buyers}
    found = []
    for row in orders:
        receipt_id = row[0]
        buyer = who.get(receipt_id)
        shipments = buyer[8] if buyer is not None else row[17]
        found.append(
            OrderReceipt(
                receipt_id=receipt_id,
                status=row[1],
                is_paid=row[2],
                is_shipped=row[3],
                grand_total=_money(row[4], row[5], row[6]),
                subtotal=_money(row[7], row[8], row[9]),
                shipping_total=_money(row[10], row[11], row[12]),
                discount_total=_money(row[13], row[14], row[15]),
                country_code=row[16],
                created_at=row[18],
                updated_at=row[19],
                lines=tuple(lines.get(receipt_id, ())),
                shipments=tuple(
                    OrderShipment(
                        carrier=s["carrier"],
                        tracking_code=s.get("tracking_code", ""),
                        notified_at=datetime.fromisoformat(s["notified_at"]),
                    )
                    for s in shipments
                ),
                refunds=tuple(refunded.get(receipt_id, ())),
                buyer_name=None if buyer is None else buyer[1],
                buyer_display_name=None if buyer is None else display_name(buyer[1]),
                shipping_address=None
                if buyer is None
                else ShippingAddress(
                    first_line=buyer[2],
                    second_line=buyer[3],
                    city=buyer[4],
                    state=buyer[5],
                    postal_code=buyer[6],
                    country_code=buyer[7],
                ),
            )
        )
    return tuple(found)
