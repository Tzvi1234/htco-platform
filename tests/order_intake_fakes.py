"""Etsy Order Intake helpers (slice 5.4): invented receipts with fake personal values, and an
order store in memory that follows the same ingest plan as Postgres (`plan_ingest`)."""

# ruff: noqa: ARG002 - the ports' signatures

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import override

from htco_application.kernel import OutboxMessage
from htco_application.marketplace import (
    MarketplaceMoney,
    OrderLine,
    OrderReceipt,
    OrderRefund,
    OrderShipment,
    ShippingAddress,
)
from htco_application.order_ingest import (
    BACKFILL,
    SYNC,
    Drift,
    IngestContext,
    IngestPlan,
    StoredOrder,
    Transition,
    observed_facts,
    plan_ingest,
)
from htco_application.shop_integrations import ShopIntegration
from tests.order_history_fakes import MemoryOrderStore

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
FAKE_NAME = "Fakeintake Zzbuyer"
FAKE_LINE = "FAKE-INTAKE-LINE-ZZZ"
FAKE_TRACKING = "FAKE-INTAKE-TRACK-01"
FAKE_NOTE = "FAKE-INTAKE-NOTE"


def usd(cents: int) -> MarketplaceMoney:
    return MarketplaceMoney(amount=cents, divisor=100, currency_code="USD")


def paid_receipt(
    receipt_id: int = 4000000001, *, minutes: int = 0, **changes: object
) -> OrderReceipt:
    created = NOW - timedelta(days=1)
    base = OrderReceipt(
        receipt_id=receipt_id,
        status="paid",
        is_paid=True,
        is_shipped=False,
        created_at=created,
        updated_at=created + timedelta(minutes=minutes),
        grand_total=usd(2599),
        subtotal=usd(2000),
        shipping_total=usd(599),
        discount_total=usd(0),
        lines=(
            OrderLine(receipt_id * 10, 777, "Fake wallet", "FAKE-W", 2, usd(1000), False, None),
            OrderLine(receipt_id * 10 + 1, 778, "Fake belt", None, 1, usd(0), False, None),
        ),
        shipments=(),
        country_code="US",
        buyer_name=FAKE_NAME,
        buyer_display_name="Fakeintake Z.",
        shipping_address=ShippingAddress(FAKE_LINE, None, "Fakecity", "FK", "00000", "US"),
        refunds=(),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def shipped(receipt: OrderReceipt, *, minutes: int) -> OrderReceipt:
    return replace(
        receipt,
        status="completed",
        is_shipped=True,
        updated_at=receipt.created_at + timedelta(minutes=minutes),
        shipments=(OrderShipment("fake-post", FAKE_TRACKING, receipt.created_at),),
    )


def refunded(receipt: OrderReceipt, *, minutes: int) -> OrderReceipt:
    return replace(
        receipt,
        status="fully refunded",
        updated_at=receipt.created_at + timedelta(minutes=minutes),
        refunds=(OrderRefund(usd(2599), receipt.created_at, "damaged", "refunded", FAKE_NOTE),),
    )


@dataclass
class MemoryIntakeStore(MemoryOrderStore):
    """`MemoryOrderStore` whose every write follows `plan_ingest`, recording what it wrote."""

    stored: dict[tuple[uuid.UUID, int], StoredOrder] = field(default_factory=dict)
    transitions: list[tuple[int, Transition, str]] = field(default_factory=list)
    events: dict[str, OutboxMessage] = field(default_factory=dict)
    plans: list[IngestPlan] = field(default_factory=list)
    cursors: list[tuple[uuid.UUID, str, datetime, str | None]] = field(default_factory=list)
    drifts: list[tuple[int, Drift]] = field(default_factory=list)
    now: datetime = NOW

    def _ingest(
        self, integration: ShopIntegration, receipts: Sequence[OrderReceipt], context: IngestContext
    ) -> list[IngestPlan]:
        written: list[IngestPlan] = []
        for receipt in receipts:
            key = (integration.shop_id, receipt.receipt_id)
            plan = plan_ingest(
                receipt,
                self.stored.get(key),
                context,
                organization_id=integration.organization_id,
                shop_id=integration.shop_id,
                now=self.now,
            )
            written.append(plan)
            self.plans.append(plan)
            self.drifts.extend((receipt.receipt_id, d) for d in plan.drifts)
            for event in plan.events:
                self.events.setdefault(event.idempotency_key, event)
            if not plan.write:
                continue
            self.orders[key] = replace(receipt, buyer_display_name=None)
            self.stored[key] = StoredOrder(
                receipt.updated_at, plan.state, plan.receipt_sha256, observed_facts(receipt)
            )
            for transition in plan.transitions:
                self.transitions.append((receipt.receipt_id, transition, context.source.value))
        return written

    async def order_count(self, shop_id: uuid.UUID, etsy_shop_id: int) -> int:
        return sum(1 for shop, _ in self.orders if shop == shop_id)

    async def record_cursor(
        self,
        integration: ShopIntegration,
        kind: str,
        *,
        started_at: datetime,
        error: str | None,
    ) -> None:
        self.cursors.append((integration.shop_id, kind, started_at, error))

    @override
    def _upsert(self, integration: ShopIntegration, receipts: Sequence[OrderReceipt]) -> None:
        if self.crash_after_pages is not None and self.pages_saved >= self.crash_after_pages:
            raise ConnectionError("fake crash")
        self.pages_saved += 1
        context = self.contexts[-1] if self.contexts else SYNC
        self._ingest(integration, receipts, context)

    async def ingest_receipts(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        context: IngestContext,
        now: datetime,
    ) -> tuple[IngestPlan, ...]:
        return tuple(self._ingest(integration, receipts, context))


__all__ = ["BACKFILL", "SYNC", "MemoryIntakeStore", "paid_receipt", "refunded", "shipped"]
