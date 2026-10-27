"""Suppliers and quotes on Postgres (slices 7.13, 7.15), as the worker in its job's organization.

Synchronous; run in worker threads. `record` writes one parsed quote in one transaction: its
supplier, its product's open quote request and the quote, each found or created once.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg

from htco_application.quote_board import OPEN_STATES, QuoteRow
from htco_application.suppliers import RecordedQuote
from htco_domain.suppliers import ProductRef, SupplierQuote
from htco_persistence.database import Database

_OPEN = "state not in ('mapped', 'cancelled')"
_PRODUCT = "organization_id = %s and shop_id = %s and listing_id = %s and variant = %s"


def _one(connection: psycopg.Connection[Any], sql: str, params: list[object]) -> Any:
    row = connection.execute(sql, params).fetchone()
    return None if row is None else row[0]


class PostgresSupplierQuoteStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record(
        self,
        organization_id: uuid.UUID,
        product: ProductRef,
        supplier_name: str,
        quote: SupplierQuote,
        *,
        now: datetime,
    ) -> RecordedQuote:
        key = [organization_id, product.shop_id, product.listing_id, product.variant]
        with self._database.connection() as connection, connection.transaction():
            supplier_id = self._supplier(connection, organization_id, supplier_name)
            alternative = (
                _one(
                    connection,
                    f"select 1 from app.supplier_mappings where {_PRODUCT} and ended_at is null",  # noqa: S608 - constants
                    key,
                )
                is not None
            )
            request_id = self._open_request(connection, key, now)
            created = (
                _one(
                    connection,
                    """
                    insert into app.supplier_quotes
                      (id, organization_id, quote_request_id, supplier_id, currency, unit_price,
                       shipping_cost, shipping_method, processing_days_min, processing_days_max,
                       shipping_days_min, shipping_days_max, valid_until, source_reference,
                       received_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (quote_request_id, source_reference) do nothing
                    returning id
                    """,
                    [
                        quote.id,
                        organization_id,
                        request_id,
                        supplier_id,
                        quote.unit_price.currency,
                        quote.unit_price.amount,
                        quote.shipping_cost.amount,
                        quote.shipping_method,
                        *quote.processing_days,
                        *quote.shipping_days,
                        quote.valid_until,
                        quote.source_reference,
                        quote.received_at,
                    ],
                )
                is not None
            )
        return RecordedQuote(
            quote_request_id=request_id,
            supplier_id=supplier_id,
            created=created,
            alternative=alternative,
        )

    @staticmethod
    def _supplier(
        connection: psycopg.Connection[Any], organization_id: uuid.UUID, name: str
    ) -> uuid.UUID:
        """The supplier AutoVA names `name` (by its AutoVA name, then its name), added if new."""
        found = _one(
            connection,
            "select id from app.suppliers where organization_id = %s"
            " and (autova_name = %s or name = %s) order by (autova_name = %s) desc nulls last"
            " limit 1",
            [organization_id, name, name, name],
        )
        if found is not None:
            return uuid.UUID(str(found))
        connection.execute(
            "insert into app.suppliers (id, organization_id, name, autova_name)"
            " values (%s, %s, %s, %s) on conflict (organization_id, name) do nothing",
            [uuid.uuid7(), organization_id, name, name],
        )
        return uuid.UUID(
            str(
                _one(
                    connection,
                    "select id from app.suppliers where organization_id = %s and name = %s",
                    [organization_id, name],
                )
            )
        )

    @staticmethod
    def _open_request(
        connection: psycopg.Connection[Any], key: list[object], now: datetime
    ) -> uuid.UUID:
        """The product's open quote request, created for its first quote; a request still
        waiting for quotes moves to `quotes_received`."""
        row = connection.execute(
            f"select id, state from app.quote_requests where {_PRODUCT} and {_OPEN} for update",  # noqa: S608 - constants
            key,
        ).fetchone()
        if row is None:
            connection.execute(
                f"""
                insert into app.quote_requests
                  (id, organization_id, shop_id, listing_id, variant, state, updated_at)
                values (%s, %s, %s, %s, %s, 'quotes_received', %s)
                on conflict (organization_id, shop_id, listing_id, variant) where {_OPEN}
                do nothing
                """,  # noqa: S608 - constants
                [uuid.uuid7(), *key, now],
            )
            row = connection.execute(
                f"select id, state from app.quote_requests where {_PRODUCT} and {_OPEN}",  # noqa: S608 - constants
                key,
            ).fetchone()
        assert row is not None  # noqa: S101 - inserted or found in this transaction
        request_id, state = uuid.UUID(str(row[0])), row[1]
        if state == "waiting_for_quotes":
            connection.execute(
                "update app.quote_requests set state = 'quotes_received', updated_at = %s"
                " where id = %s",
                [now, request_id],
            )
        return request_id


# At most this many rows per board: a shop has a handful of products waiting at a time.
BOARD_LIMIT = 1000


class PostgresQuoteBoardReader:
    """The "New product quotes" board (slice 7.16), read as the signed-in user: row-level
    security keeps it to the caller's organization and shops."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def open_quotes(self, claims: Mapping[str, Any], shop_id: uuid.UUID) -> list[QuoteRow]:
        with self._database.user_transaction(claims) as connection:
            rows = connection.execute(
                """
                select r.id, r.listing_id, r.variant, r.state, r.created_at,
                       q.id, s.name, q.currency, q.unit_price, q.shipping_cost,
                       q.shipping_method, q.processing_days_min, q.processing_days_max,
                       q.shipping_days_min, q.shipping_days_max, q.valid_until, q.received_at
                from app.quote_requests r
                left join app.supplier_quotes q
                  on q.quote_request_id = r.id and q.organization_id = r.organization_id
                left join app.suppliers s
                  on s.id = q.supplier_id and s.organization_id = q.organization_id
                where r.shop_id = %s and r.state = any(%s)
                order by r.created_at, r.id, q.received_at
                limit %s
                """,
                [shop_id, list(OPEN_STATES), BOARD_LIMIT],
            ).fetchall()
        return [_board_row(row) for row in rows]


def _board_row(row: tuple[Any, ...]) -> QuoteRow:
    has_quote = row[5] is not None
    return QuoteRow(
        request_id=row[0], listing_id=int(row[1]), variant=row[2], state=row[3],
        requested_at=row[4], quote_id=row[5], supplier=row[6], currency=row[7],
        unit_price=row[8], shipping_cost=row[9], shipping_method=row[10],
        processing_days=(row[11], row[12]) if has_quote else None,
        shipping_days=(row[13], row[14]) if has_quote else None,
        valid_until=row[15], received_at=row[16],
    )  # fmt: skip
