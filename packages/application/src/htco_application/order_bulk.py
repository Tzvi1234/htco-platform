"""The Orders screen's bulk actions (slice 5.7; the owner's decision of 2026-10-23).

**Export:** the filtered orders (and the selection, when one is made) as CSV, generated on the
server. Buyer name and address columns, and tracking numbers, only for the roles that see buyer
data (Owner, Admin, Operations, Support); for every other role those columns are left out, not
blanked. Every cell that a spreadsheet would read as a formula (`=`, `+`, `-`, `@`, tab, carriage
return) starts with an apostrophe (OWASP CSV injection). Each export is audited with its counts
and filter only.

**Re-sync selected:** one Etsy Order Intake re-read per receipt, queued as `etsy.order_sync`
jobs that the worker runs through the engine and the Action Gateway (GET-only). At most
`MAX_RESYNC` receipts per request and `HTCO`'s per-shop window per minute; audited with the
receipt IDs (no buyer data).
"""

import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from htco_application.access.authentication import AuthContext
from htco_application.audit import AuditRecord
from htco_application.marketplace import OrderReceipt
from htco_application.shop_dashboard import (
    OrderFilter,
    OrderPeriod,
    OrderView,
    ShopDashboard,
    orders_for_export,
    resync_target,
)
from htco_domain.actors import ActorKind, ActorRef

MAX_RESYNC = 50
BASE_COLUMNS = (
    "order", "placed_at", "state", "total", "currency", "units", "items", "skus", "ship_by",
    "shipped_at", "carrier",
)  # fmt: skip
TRACKING_COLUMN = "tracking"
BUYER_COLUMNS = (
    "buyer_name", "address_line_1", "address_line_2", "city", "region", "postal_code",
    "country",
)  # fmt: skip
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


class OrdersForbiddenError(Exception):
    """The role may see the orders but not run this action on them."""


class ExportUnavailableError(Exception):
    """Exports are audited; without the audit log none is made."""


class ResyncUnavailableError(Exception):
    """No queue on this server for the re-reads."""


class ResyncRateLimitedError(Exception):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__("too many re-syncs for this shop")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class OrderExport:
    filename: str
    text: str
    orders: int
    buyer_columns: bool


def _safe(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


def _row(receipt: OrderReceipt, view: OrderView, *, buyer: bool) -> list[str]:
    shipped = view.shipments[0] if view.shipments else None
    cells: list[object] = [
        view.order_id,
        view.created_at.isoformat(),
        "late" if view.overdue else view.state.value,
        view.total.amount,
        view.total.currency,
        view.units,
        "; ".join(f"{item.title} x{item.quantity}" for item in view.items),
        "; ".join(item.sku for item in view.items if item.sku),
        "" if view.ship_by is None else view.ship_by.isoformat(),
        "" if shipped is None else shipped.shipped_at.isoformat(),
        "" if shipped is None else shipped.carrier,
    ]
    if buyer:
        address = receipt.shipping_address
        cells.append("" if shipped is None else (shipped.tracking_code or ""))
        cells.extend(
            [
                receipt.buyer_name,
                None if address is None else address.first_line,
                None if address is None else address.second_line,
                None if address is None else address.city,
                None if address is None else address.state,
                None if address is None else address.postal_code,
                None if address is None else address.country_code,
            ]
        )
    return [_safe(cell) for cell in cells]


def render_csv(rows: Iterable[tuple[OrderReceipt, OrderView]], *, buyer: bool) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([*BASE_COLUMNS, *((TRACKING_COLUMN, *BUYER_COLUMNS) if buyer else ())])
    for receipt, view in rows:
        writer.writerow(_row(receipt, view, buyer=buyer))
    return out.getvalue()


async def export_orders(  # noqa: PLR0913 - the shop, the caller, the filter and the request
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    period: OrderPeriod = OrderPeriod.DAYS_30,
    state: OrderFilter | None = None,
    order_ids: tuple[int, ...] = (),
    correlation_id: str | None = None,
) -> OrderExport:
    if dashboard.audit is None:
        raise ExportUnavailableError("exports are audited")
    source = await orders_for_export(
        dashboard, auth, shop_id, now=now, period=period, state=state, order_ids=order_ids
    )
    text = render_csv(source.rows, buyer=source.buyer_data)
    await dashboard.audit.append(
        AuditRecord.new(
            organization_id=source.organization_id,
            actor=ActorRef(ActorKind.USER, str(auth.user_id)),
            action="orders.exported",
            entity_type="shop",
            entity_id=str(shop_id),
            after={
                "orders": len(source.rows),
                "period": period.value,
                "state": None if state is None else state.value,
                "selected": len(set(order_ids)),
                "buyer_columns": source.buyer_data,
            },
            correlation_id=correlation_id,
        )
    )
    stamp = now.strftime("%Y%m%d-%H%M")
    return OrderExport(
        filename=f"orders-{period.value}-{stamp}.csv",
        text=text,
        orders=len(source.rows),
        buyer_columns=source.buyer_data,
    )


async def resync_orders(  # noqa: PLR0913 - the shop, the caller, the receipts and the request
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    receipt_ids: tuple[int, ...],
    *,
    now: datetime,
    correlation_id: str,
) -> int:
    unique = tuple(dict.fromkeys(receipt_ids))
    if not 1 <= len(unique) <= MAX_RESYNC or any(r <= 0 for r in unique):
        raise ValueError(f"choose between 1 and {MAX_RESYNC} orders")
    integration, allowed = await resync_target(dashboard, auth, shop_id, now=now)
    if not allowed:
        raise OrdersForbiddenError("this role does not re-sync orders")
    if dashboard.resync is None or dashboard.resync_limiter is None or dashboard.audit is None:
        raise ResyncUnavailableError("re-syncs are not set up on this server")
    wait = dashboard.resync_limiter.admit(f"resync:{shop_id}", now=now)
    if wait is not None:
        raise ResyncRateLimitedError(max(1.0, wait))
    queued = await dashboard.resync.enqueue_receipts(
        integration, unique, now=now, correlation_id=correlation_id
    )
    await dashboard.audit.append(
        AuditRecord.new(
            organization_id=integration.organization_id,
            actor=ActorRef(ActorKind.USER, str(auth.user_id)),
            action="orders.resync_requested",
            entity_type="shop",
            entity_id=str(shop_id),
            after={"receipt_ids": list(unique)},
            correlation_id=correlation_id,
        )
    )
    return queued
