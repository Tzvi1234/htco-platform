"""The AutoVA relay's store (slice 6.10): `app.relay_items` and `app.relay_item_codes`.

The API uses it with its own login role (the paste and the list; the application layer decides
who may), the worker with `htco_worker` inside its job's organization (opening an item and
closing it on Etsy's tracking; it never reads a code).
"""

import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg import errors

from htco_application.audit import AuditRecord
from htco_application.relay import DuplicateTrackingError, RelayItem, RelayItemMovedError
from htco_domain.relay import ClosedReason, RelayState
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

_COLUMNS = """
  id, organization_id, shop_id, receipt_id, state, paid_at, opened_at, updated_at, carrier,
  tracking_last4, closed_reason, closed_at
"""


def _item(row: tuple[Any, ...]) -> RelayItem:
    return RelayItem(
        id=row[0],
        organization_id=row[1],
        shop_id=row[2],
        receipt_id=int(row[3]),
        state=RelayState(row[4]),
        paid_at=row[5],
        opened_at=row[6],
        updated_at=row[7],
        carrier=row[8],
        last4=row[9],
        closed_reason=None if row[10] is None else ClosedReason(row[10]),
        closed_at=row[11],
    )


class PostgresRelayStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def open_item(self, item: RelayItem) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                insert into app.relay_items
                  (id, organization_id, shop_id, receipt_id, state, paid_at, opened_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (shop_id, receipt_id) do nothing
                returning id
                """,
                [
                    item.id,
                    item.organization_id,
                    item.shop_id,
                    item.receipt_id,
                    item.state.value,
                    item.paid_at,
                    item.opened_at,
                    item.updated_at,
                ],
            ).fetchone()
        return row is not None

    def item(self, organization_id: uuid.UUID, item_id: uuid.UUID) -> RelayItem | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"select {_COLUMNS} from app.relay_items where organization_id = %s and id = %s",  # noqa: S608 - a constant column list
                [organization_id, item_id],
            ).fetchone()
        return None if row is None else _item(row)

    def open_items(
        self, organization_id: uuid.UUID, *, shop_ids: frozenset[uuid.UUID] | None, limit: int
    ) -> tuple[RelayItem, ...]:
        shops = None if shop_ids is None else list(shop_ids)
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_COLUMNS} from app.relay_items
                where organization_id = %(org)s and state <> 'closed'
                  and (%(shops)s::uuid[] is null or shop_id = any(%(shops)s::uuid[]))
                order by opened_at, id
                limit %(limit)s
                """,  # noqa: S608 - a constant column list
                {"org": organization_id, "shops": shops, "limit": limit},
            ).fetchall()
        return tuple(_item(r) for r in rows)

    def code(self, organization_id: uuid.UUID, item_id: uuid.UUID) -> str | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select code from app.relay_item_codes
                where organization_id = %s and relay_item_id = %s
                """,
                [organization_id, item_id],
            ).fetchone()
        return None if row is None else str(row[0])

    def tracking_on_receipt(self, shop_id: uuid.UUID, receipt_id: int, fingerprint: bytes) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select 1 from app.tracking_events
                where shop_id = %s and receipt_id = %s and fingerprint = %s
                """,
                [shop_id, receipt_id, fingerprint],
            ).fetchone()
        return row is not None

    def hand_off(  # noqa: PLR0913 - the item, the tracking and the audit
        self,
        item_id: uuid.UUID,
        *,
        carrier: str,
        fingerprint: bytes,
        last4: str,
        code: str,
        now: datetime,
        audit: AuditRecord,
    ) -> RelayItem:
        try:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    f"""
                    update app.relay_items
                    set state = 'handed_off', carrier = %s, tracking_fingerprint = %s,
                        tracking_last4 = %s, updated_at = %s
                    where id = %s and state = 'awaiting_tracking'
                    returning {_COLUMNS}
                    """,  # noqa: S608 - a constant column list
                    [carrier, fingerprint, last4, now, item_id],
                ).fetchone()
                if row is None:
                    state = connection.execute(
                        "select state from app.relay_items where id = %s", [item_id]
                    ).fetchone()
                    raise RelayItemMovedError(RelayState(state[0]) if state else RelayState.CLOSED)
                moved = _item(row)
                connection.execute(
                    """
                    insert into app.relay_item_codes (relay_item_id, organization_id, shop_id, code)
                    values (%s, %s, %s, %s)
                    """,
                    [moved.id, moved.organization_id, moved.shop_id, code],
                )
                append_audit(connection, audit)
        except errors.UniqueViolation as error:
            if _constraint(error) == "relay_items_fingerprint_key":
                raise DuplicateTrackingError from None
            raise
        return moved

    def close(
        self,
        item_id: uuid.UUID,
        *,
        reason: ClosedReason,
        now: datetime,
        audit: AuditRecord | None = None,
    ) -> RelayItem | None:
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                f"""
                update app.relay_items
                set state = 'closed', closed_reason = %s, closed_at = %s, updated_at = %s
                where id = %s and state <> 'closed'
                returning {_COLUMNS}
                """,  # noqa: S608 - a constant column list
                [reason.value, now, now, item_id],
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "delete from app.relay_item_codes where relay_item_id = %s", [item_id]
            )
            if audit is not None:
                append_audit(connection, audit)
        return _item(row)

    def close_order(
        self, shop_id: uuid.UUID, receipt_id: int, *, reason: ClosedReason, now: datetime
    ) -> bool:
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                update app.relay_items
                set state = 'closed', closed_reason = %s, closed_at = %s, updated_at = %s
                where shop_id = %s and receipt_id = %s and state <> 'closed'
                returning id
                """,
                [reason.value, now, now, shop_id, receipt_id],
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "delete from app.relay_item_codes where relay_item_id = %s", [row[0]]
            )
        return True


def _constraint(error: psycopg.Error) -> str | None:
    diag = getattr(error, "diag", None)
    return None if diag is None else diag.constraint_name
