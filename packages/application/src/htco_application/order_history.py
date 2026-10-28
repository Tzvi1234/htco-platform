"""The shop's stored order history: a one-time backfill and a rolling 30-day sync (owner's
decision of 2026-10-22).

The Orders and Statistics screens read the shop's whole order history from HTCO's database once
the backfill is complete; until then they keep reading Etsy live and say the history is syncing.

- **Backfill:** once per shop, every receipt since the shop opened, page by page (100 a page,
  one after the other, no cap) through the read-only adapter, which paces each call and waits
  out a 429. Each page is upserted by `(shop_id, receipt_id)` together with the next offset in
  one transaction, so a crash or restart resumes from the checkpoint and a replay changes
  nothing. Etsy lists newest first: orders placed during the backfill push older ones to later
  offsets, which only repeats a few (upserted again), never skips one.
- **Incremental:** every receipt *changed* in the last 30 days (`min_last_modified`), whenever
  it was ordered, read again and upserted, so new orders, status changes, shipments, refunds
  and cancellations land in the store, also a late refund of an older order (post-merge review
  of 2026-10-23: reading by creation date missed those).
- **No worker yet** (Phase 5): a read of Orders, Statistics or Overview starts a background task
  in the API process when the backfill is missing or the last sync is older than 10 minutes,
  never blocking the request. A Postgres advisory lock per shop lets one run at a time across
  instances. The Owner or an Admin can also start one (`request_order_sync`, audited).

Personal data: the buyer's name and shipping address are stored in their own table, read only
for roles that may see buyer details (`view_buyer_contact`, unmasked `view_orders`), and never
logged, reported or audited. Email, phone, messages, gift messages and notes are never read.
Etsy is read only: every call is a GET through the guarded adapter.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.kernel import Page
from htco_application.marketplace import (
    AccessTokenProvider,
    MarketplaceRateLimitedError,
    OrderReceipt,
)
from htco_application.order_ingest import BACKFILL, SYNC, IngestContext, IngestSource
from htco_application.shop_capabilities import ShopNotConnectedError, TokenPinning
from htco_application.shop_integrations import ShopIntegration
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import IntegrationStatus
from htco_domain.orders import OrderState

_log = logging.getLogger(__name__)

PAGE = 100
# The incremental sync reads the last 30 days again.
INCREMENTAL_DAYS = 30
# A read starts a sync when the last one is older than this.
FRESH_FOR = timedelta(minutes=10)
# A started run is not started again in this process sooner than this (a failing Etsy or a run
# held by another instance is not retried on every page view).
RETRY_AFTER = timedelta(seconds=60)
# Stored data is marked stale when no sync succeeded for this long.
STALE_AFTER = timedelta(minutes=30)
# A 429 that outlasted the adapter's own retries: wait and retry the page this many times.
RATE_LIMIT_RETRIES = 5
MAX_RATE_LIMIT_WAIT = 60.0


class HistoryState(StrEnum):
    # The backfill has not finished: reads fall back to Etsy live.
    SYNCING_HISTORY = "syncing_history"
    # The whole history is stored: reads come from the store.
    COMPLETE = "complete"


@dataclass(frozen=True)
class SyncState:
    """Where a shop's order history stands. No buyer data."""

    shop_id: uuid.UUID
    etsy_shop_id: int
    # The backfill's checkpoint: the offset of the next page to read.
    backfill_offset: int
    backfill_completed_at: datetime | None
    incremental_synced_at: datetime | None
    orders_stored: int
    last_error: str | None = None

    def complete_for(self, etsy_shop_id: int) -> bool:
        return self.backfill_completed_at is not None and self.etsy_shop_id == etsy_shop_id

    @property
    def last_synced_at(self) -> datetime | None:
        moments = [m for m in (self.backfill_completed_at, self.incremental_synced_at) if m]
        return max(moments) if moments else None


@dataclass(frozen=True)
class HistorySync:
    """What the Orders and Statistics answers say about the stored history."""

    state: HistoryState
    orders_stored: int
    last_synced_at: datetime | None


@dataclass(frozen=True)
class OrderStatusChange:
    """One recorded transition of an order (`order_status_events`): no buyer data."""

    from_state: OrderState | None
    to_state: OrderState
    source: IngestSource
    observed_at: datetime


@dataclass(frozen=True)
class OrderTracking:
    """One parcel's tracking as HTCO first saw it (`tracking_events`, AC-6.1.2): the carrier, the
    code's last four characters only (the full code is buyer data), where it came from and which
    trigger saw it."""

    carrier: str  # ours (`htco_domain.fulfillment.Carrier`)
    carrier_name: str  # as Etsy names it
    last4: str
    source: str  # `tracking_sync.TrackingSource`: etsy, autova_manual, autova_browser…
    observed_via: str  # `tracking_sync.ObservedVia`: webhook, schedule, relay
    first_observed_at: datetime


# --- Ports -------------------------------------------------------------------------------------


class OrderStore(Protocol):
    """The order history's database (`htco_persistence.etsy_orders`)."""

    async def state(self, shop_id: uuid.UUID) -> SyncState | None: ...

    async def save_backfill_page(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        next_offset: int | None,
        now: datetime,
        context: IngestContext = BACKFILL,
    ) -> None:
        """Upsert the page and move the checkpoint in one transaction; `next_offset` None
        completes the backfill. A new Etsy shop behind the HTCO shop starts the state over."""
        ...

    async def save_incremental_page(
        self,
        integration: ShopIntegration,
        receipts: Sequence[OrderReceipt],
        *,
        finished_at: datetime | None,
        now: datetime,
        context: IngestContext = SYNC,
    ) -> None:
        """Upsert the page; `finished_at` set: the sync that started then is complete."""
        ...

    async def record_failure(self, shop_id: uuid.UUID, *, error: str, now: datetime) -> None:
        """`error` is an exception type name, never a message."""
        ...

    async def load(
        self,
        shop_id: uuid.UUID,
        etsy_shop_id: int,
        *,
        since: datetime | None,
        buyer_data: bool,
    ) -> tuple[OrderReceipt, ...]:
        """Stored orders created since `since` (None: all), newest first. Without `buyer_data`
        the buyer table is not read: no name, no address, no tracking number."""
        ...

    async def load_order(
        self, shop_id: uuid.UUID, etsy_shop_id: int, receipt_id: int, *, buyer_data: bool
    ) -> OrderReceipt | None:
        """One stored order, under the same buyer-data rule as `load`."""
        ...

    async def timeline(self, shop_id: uuid.UUID, receipt_id: int) -> tuple[OrderStatusChange, ...]:
        """The order's recorded transitions, oldest first (causal order)."""
        ...

    async def tracking(self, shop_id: uuid.UUID, receipt_id: int) -> tuple[OrderTracking, ...]:
        """The order's parcels as first seen (`tracking_events`), oldest first; no full code."""
        ...

    def lock(self, shop_id: uuid.UUID) -> AbstractAsyncContextManager[bool]:
        """Try the shop's advisory lock without waiting; yields whether it is held."""
        ...


class ReceiptSource(Protocol):
    async def list_order_receipts(  # noqa: PLR0913 - the adapter's read and its token source
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        min_last_modified: datetime | None = None,
        limit: int = PAGE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[OrderReceipt]: ...


class ReceiptPages(Protocol):
    """One shop's receipts, page by page, for one run (#147, AC-3): the Etsy adapter with the
    shop's pinned tokens (`adapter_pages`, the API process's fallback), or the Action Gateway
    (`etsy_reads.OrderHistoryRead`, the worker's Etsy Order Intake)."""

    async def page(self, *, changed_since: datetime | None, offset: int) -> Page[OrderReceipt]: ...


@dataclass(frozen=True)
class AdapterReceiptPages:
    source: ReceiptSource
    etsy_shop_id: int
    tokens: AccessTokenProvider

    async def page(self, *, changed_since: datetime | None, offset: int) -> Page[OrderReceipt]:
        return await self.source.list_order_receipts(
            self.etsy_shop_id,
            min_created=None,
            min_last_modified=changed_since,
            limit=PAGE,
            offset=offset,
            tokens=self.tokens,
        )


def adapter_pages(
    source: ReceiptSource, tokens: TokenPinning
) -> Callable[[ShopIntegration], ReceiptPages]:
    """The adapter's pages for a run, with the shop's tokens pinned to its connection."""

    def pages(integration: ShopIntegration) -> ReceiptPages:
        pinned = tokens.pinned(integration.shop_id, generation=integration.generation)
        return AdapterReceiptPages(source, integration.etsy_shop_id, pinned)

    return pages


class SyncRequests(Protocol):
    """Asks the worker for a run of the shop's order history sync (slice 5.4)."""

    async def request(self, integration: ShopIntegration, *, now: datetime, force: bool) -> bool:
        """Enqueue the run (once per period; a forced one once per minute). True: asked."""
        ...


class AuditAppender(Protocol):
    async def append(self, record: AuditRecord) -> None: ...


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# --- The sync ----------------------------------------------------------------------------------


class OrderSync:
    """Runs the backfill and the incremental sync in the API process, one run per shop at a
    time (in this process by a task registry, across instances by the store's advisory lock)."""

    def __init__(  # noqa: PLR0913 - the collaborators, built by the composition root
        self,
        *,
        store: OrderStore,
        pages: Callable[[ShopIntegration], ReceiptPages],
        clock: Callable[[], datetime],
        audit: AuditAppender | None = None,
        sleep: Callable[[float], Awaitable[None]] = _sleep,
        requests: SyncRequests | None = None,
        backfill_context: IngestContext = BACKFILL,
        sync_context: IngestContext = SYNC,
    ) -> None:
        self.store = store
        # Set: the runs belong to the worker (slice 5.4); this process only asks for them.
        self.requests = requests
        self.backfill_context = backfill_context
        self.sync_context = sync_context
        self._pages = pages
        self._clock = clock
        self.audit = audit
        self._sleep = sleep
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._started: dict[uuid.UUID, datetime] = {}

    # Lifecycle (an API resource): running syncs stop with the process and resume from their
    # checkpoint on the next start.
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def idle(self) -> None:
        """Wait for the running syncs (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def running(self, shop_id: uuid.UUID) -> bool:
        task = self._tasks.get(shop_id)
        return task is not None and not task.done()

    async def ensure(
        self, integration: ShopIntegration, *, now: datetime, force: bool = False
    ) -> SyncState | None:
        """The shop's state; starts a background run when one is due. Never waits for it."""
        state = await self.store.state(integration.shop_id)
        if force or _due(state, integration, now):
            await self.start_or_request(integration, now=now, force=force)
        return state

    async def start_or_request(
        self, integration: ShopIntegration, *, now: datetime, force: bool = False
    ) -> bool:
        """Run here (the fallback), or ask the worker's queue for the run (slice 5.4)."""
        if self.requests is not None:
            return await self.requests.request(integration, now=now, force=force)
        return self.kick(integration, now=now, force=force)

    def kick(self, integration: ShopIntegration, *, now: datetime, force: bool = False) -> bool:
        shop_id = integration.shop_id
        if self.running(shop_id):
            return False
        started = self._started.get(shop_id)
        if not force and started is not None and now - started < RETRY_AFTER:
            return False
        self._started[shop_id] = now
        task = asyncio.create_task(self._guarded(integration))
        self._tasks[shop_id] = task
        task.add_done_callback(lambda _: self._forget(shop_id, task))
        return True

    def _forget(self, shop_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(shop_id) is task:
            del self._tasks[shop_id]

    async def _guarded(self, integration: ShopIntegration) -> None:
        try:
            await self.run(integration)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The type only: a database error's message can quote a row (buyer data).
            name = type(error).__name__
            _log.warning("order_history.sync_failed: %s", name)
            try:
                await self.store.record_failure(integration.shop_id, error=name, now=self._clock())
            except Exception as again:
                _log.warning("order_history.failure_not_recorded: %s", type(again).__name__)

    async def run(self, integration: ShopIntegration) -> bool:
        """One run: the backfill from its checkpoint when unfinished, then the incremental sync.
        False when another instance holds the shop's lock."""
        async with self.store.lock(integration.shop_id) as held:
            if not held:
                return False
            state = await self.store.state(integration.shop_id)
            pages = self._pages(integration)
            if state is None or not state.complete_for(integration.etsy_shop_id):
                offset = 0
                if state is not None and state.etsy_shop_id == integration.etsy_shop_id:
                    offset = state.backfill_offset
                await self._backfill(integration, pages, offset)
            await self._incremental(integration, pages)
            return True

    async def _backfill(
        self, integration: ShopIntegration, pages: ReceiptPages, offset: int
    ) -> None:
        next_offset: int | None = offset
        while next_offset is not None:
            page = await self._read(pages, offset=next_offset)
            next_offset = page.next_offset
            await self.store.save_backfill_page(
                integration,
                page.items,
                next_offset=next_offset,
                now=self._clock(),
                context=self.backfill_context,
            )

    async def _incremental(self, integration: ShopIntegration, pages: ReceiptPages) -> None:
        started = self._clock()
        since = started - timedelta(days=INCREMENTAL_DAYS)
        offset: int | None = 0
        while offset is not None:
            page = await self._read(pages, changed_since=since, offset=offset)
            offset = page.next_offset
            await self.store.save_incremental_page(
                integration,
                page.items,
                finished_at=started if offset is None else None,
                now=self._clock(),
                context=self.sync_context,
            )

    async def _read(
        self, pages: ReceiptPages, *, changed_since: datetime | None = None, offset: int
    ) -> Page[OrderReceipt]:
        """One page, routine (never urgent). A 429 that outlasted the adapter's own waits is
        waited out here a few times; any other failure ends the run (the checkpoint stays)."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return await pages.page(changed_since=changed_since, offset=offset)
            except MarketplaceRateLimitedError as error:
                if attempt == RATE_LIMIT_RETRIES:
                    raise
                wait = error.retry_after_seconds or float(2**attempt)
                await self._sleep(min(MAX_RATE_LIMIT_WAIT, max(1.0, wait)))
        raise AssertionError("unreachable")  # pragma: no cover


def _due(state: SyncState | None, integration: ShopIntegration, now: datetime) -> bool:
    if state is None or not state.complete_for(integration.etsy_shop_id):
        return True
    synced = state.incremental_synced_at
    return synced is None or now - synced >= FRESH_FOR


def history_sync(state: SyncState | None, etsy_shop_id: int) -> HistorySync:
    complete = state is not None and state.complete_for(etsy_shop_id)
    return HistorySync(
        state=HistoryState.COMPLETE if complete else HistoryState.SYNCING_HISTORY,
        orders_stored=0 if state is None else state.orders_stored,
        last_synced_at=None if state is None else state.last_synced_at,
    )


# --- The manual sync ---------------------------------------------------------------------------


class OrderSyncConnections(Protocol):
    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None: ...


async def request_order_sync(  # noqa: PLR0913 - the sync, the caller, the shop and context
    sync: OrderSync,
    connections: OrderSyncConnections,
    auth: AuthContext,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    correlation_id: str | None,
) -> HistorySync:
    """The Owner or an Admin starts a sync of the shop's order history now (organization-wide
    `request_bulk_resync`). Audited with the trigger only; answers at once, the run goes on in
    the background. Raises `ShopNotFoundError`, `ShopAccessDeniedError` or
    `ShopNotConnectedError`."""
    integration = await connections.describe(shop_id)
    if integration is None:
        raise ShopNotFoundError("no such shop")
    try:
        principal = auth.principal_for(integration.organization_id)
    except NoMembershipError:
        raise ShopNotFoundError("no such shop") from None
    shop = ShopRef(id=shop_id, organization_id=integration.organization_id)
    seen = decide(principal, Capability.VIEW_OVERVIEW, shop=shop, now=now)
    if seen.outcome is not Outcome.ALLOW:
        raise ShopNotFoundError("no such shop")
    decision = decide(principal, Capability.REQUEST_BULK_RESYNC, shop=shop, now=now)
    if decision.outcome is not Outcome.ALLOW or decision.scope is not Scope.ORGANIZATION:
        raise ShopAccessDeniedError("only the Owner and Admins sync the order history")
    if integration.status is not IntegrationStatus.CONNECTED:
        raise ShopNotConnectedError.of(integration)
    state = await sync.store.state(shop_id)
    started = await sync.start_or_request(integration, now=now, force=True)
    if sync.audit is not None:
        await sync.audit.append(
            AuditRecord.new(
                organization_id=integration.organization_id,
                actor=ActorRef(ActorKind.USER, str(auth.user_id)),
                action="shop.order_sync_requested",
                entity_type="shop",
                entity_id=str(shop_id),
                after={"started": started},
                correlation_id=correlation_id,
            )
        )
    return history_sync(state, integration.etsy_shop_id)
