"""The order history's store in memory, with the database's upsert rules (invented values only).

Shared by the application and API tests; the Postgres store is tested against the same rules in
`tests/integration/test_order_history.py`.
"""

# ruff: noqa: ARG002 - the port's signatures

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime

from htco_application.marketplace import OrderReceipt, display_name
from htco_application.order_history import OrderStatusChange, OrderTracking, SyncState
from htco_application.order_ingest import BACKFILL, SYNC, IngestContext
from htco_application.shop_integrations import ShopIntegration


@dataclass
class _State:
    etsy_shop_id: int
    offset: int = 0
    completed_at: datetime | None = None
    incremental_at: datetime | None = None
    last_error: str | None = None


@dataclass
class MemoryOrderStore:
    orders: dict[tuple[uuid.UUID, int], OrderReceipt] = field(default_factory=dict)
    states: dict[uuid.UUID, _State] = field(default_factory=dict)
    held: set[uuid.UUID] = field(default_factory=set)
    # Every `load` call's `buyer_data` flag.
    buyer_reads: list[bool] = field(default_factory=list)
    # Fail the save after this many pages (simulates a crash mid-backfill).
    crash_after_pages: int | None = None
    pages_saved: int = 0
    # Every save's ingest context (who wrote the page, slice 5.4).
    contexts: list[IngestContext] = field(default_factory=list)
    # Recorded transitions per order (slice 5.6), in the order they were recorded.
    timelines: dict[tuple[uuid.UUID, int], list[OrderStatusChange]] = field(default_factory=dict)
    # Tracking first seen per order (`tracking_events`, AC-6.1.2).
    parcels: dict[tuple[uuid.UUID, int], list[OrderTracking]] = field(default_factory=dict)

    async def state(self, shop_id: uuid.UUID) -> SyncState | None:
        found = self.states.get(shop_id)
        if found is None:
            return None
        stored = sum(1 for (shop, _) in self.orders if shop == shop_id)
        return SyncState(
            shop_id=shop_id,
            etsy_shop_id=found.etsy_shop_id,
            backfill_offset=found.offset,
            backfill_completed_at=found.completed_at,
            incremental_synced_at=found.incremental_at,
            orders_stored=stored,
            last_error=found.last_error,
        )

    def _ensure(self, integration: ShopIntegration) -> _State:
        found = self.states.get(integration.shop_id)
        if found is None or found.etsy_shop_id != integration.etsy_shop_id:
            found = _State(integration.etsy_shop_id)
            self.states[integration.shop_id] = found
        return found

    def _upsert(self, integration: ShopIntegration, receipts: Sequence[OrderReceipt]) -> None:
        if self.crash_after_pages is not None and self.pages_saved >= self.crash_after_pages:
            raise ConnectionError("fake crash")
        self.pages_saved += 1
        for receipt in receipts:
            key = (integration.shop_id, receipt.receipt_id)
            stored = self.orders.get(key)
            if stored is None or stored.updated_at <= receipt.updated_at:
                # What the database keeps: no display name of its own (made from the name).
                self.orders[key] = replace(receipt, buyer_display_name=None)

    async def save_backfill_page(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        next_offset: int | None,
        now: datetime,
        context: IngestContext = BACKFILL,
    ) -> None:
        self.contexts.append(context)
        state = self._ensure(integration)
        self._upsert(integration, receipts)
        if next_offset is None:
            state.completed_at = now
        else:
            state.offset = next_offset

    async def save_incremental_page(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        finished_at: datetime | None,
        now: datetime,
        context: IngestContext = SYNC,
    ) -> None:
        self.contexts.append(context)
        state = self._ensure(integration)
        self._upsert(integration, receipts)
        if finished_at is not None:
            state.incremental_at = finished_at

    async def record_failure(self, shop_id: uuid.UUID, *, error: str, now: datetime) -> None:
        if shop_id in self.states:
            self.states[shop_id].last_error = error

    async def load(
        self,
        shop_id: uuid.UUID,
        etsy_shop_id: int,
        *,
        since: datetime | None,
        buyer_data: bool,
    ) -> tuple[OrderReceipt, ...]:
        self.buyer_reads.append(buyer_data)
        found = [
            r
            for (shop, _), r in self.orders.items()
            if shop == shop_id and (since is None or r.created_at >= since)
        ]
        found.sort(key=lambda r: (r.created_at, r.receipt_id), reverse=True)
        if buyer_data:
            return tuple(replace(r, buyer_display_name=display_name(r.buyer_name)) for r in found)
        return tuple(
            replace(
                r,
                buyer_name=None,
                buyer_display_name=None,
                shipping_address=None,
                shipments=tuple(replace(s, tracking_code="") for s in r.shipments),
            )
            for r in found
        )

    async def load_order(
        self, shop_id: uuid.UUID, etsy_shop_id: int, receipt_id: int, *, buyer_data: bool
    ) -> OrderReceipt | None:
        every = await self.load(shop_id, etsy_shop_id, since=None, buyer_data=buyer_data)
        return next((r for r in every if r.receipt_id == receipt_id), None)

    async def timeline(self, shop_id: uuid.UUID, receipt_id: int) -> tuple[OrderStatusChange, ...]:
        found = self.timelines.get((shop_id, receipt_id), [])
        return tuple(sorted(found, key=lambda c: c.observed_at))

    async def tracking(self, shop_id: uuid.UUID, receipt_id: int) -> tuple[OrderTracking, ...]:
        found = self.parcels.get((shop_id, receipt_id), [])
        return tuple(sorted(found, key=lambda t: t.first_observed_at))

    @asynccontextmanager
    async def lock(self, shop_id: uuid.UUID) -> AsyncIterator[bool]:
        if shop_id in self.held:
            yield False
            return
        self.held.add(shop_id)
        try:
            yield True
        finally:
            self.held.discard(shop_id)


class AuditLog:
    def __init__(self) -> None:
        self.records: list[object] = []

    async def append(self, record: object) -> None:
        self.records.append(record)
