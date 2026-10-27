"""Etsy Order Intake on the durable engine (slice 5.4): the `etsy-order-intake` handler.

Every Etsy read goes through the Action Gateway (`etsy.order_history.read` by `OrderHistoryRead`,
API channel, the guarded GET-only adapter, the app-wide rate budget and the `etsy:api` breaker):
the whole receipt as the store keeps it, an R1 output the gateway never stores. Every write goes
to the #57 order history store through its one ingest path (`order_ingest`), in one transaction
with the state transitions, the estimated money and the events.

Two kinds of run, both keyed by the trigger:

- **A receipt** (a webhook, `etsy:{shop_id}:{receipt_id}:{event_type}`): re-read that receipt and
  ingest it. `order.delivered` marks it delivered when the re-read shows it shipped.
- **The history** (`mode = "history"`: the dashboard's stale read, the Owner's manual sync): the
  #57 backfill from its checkpoint, then the 30-day re-read by `min_last_modified`, under the
  shop's advisory lock shared with the API process's fallback run.

The API process no longer runs the sync when `HTCO_ORDER_SYNC_IN_API` is off: it enqueues
`etsy.order_sync` (`QueuedSyncRequests`), whose worker job triggers this automation.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Protocol

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_reads import (
    OrderHistoryRead,
)
from htco_application.etsy_webhooks import ORDER_INTAKE, TriggersAutomations
from htco_application.jobs import ClaimedJob, JobQueue, JobSpec, fan_out_per_shop
from htco_application.kernel import (
    ConnectedShops,
    JobError,
    JsonValue,
    LockKeys,
    Offload,
    Page,
    run_inline,
)
from htco_application.marketplace import (
    MarketplaceRateLimitedError,
    OrderReceipt,
)
from htco_application.order_history import (
    INCREMENTAL_DAYS,
    MAX_RATE_LIMIT_WAIT,
    RATE_LIMIT_RETRIES,
    OrderStore,
    OrderSync,
)
from htco_application.order_ingest import (
    BACKFILL,
    IngestContext,
    IngestPlan,
    IngestSource,
)
from htco_application.shop_integrations import ShopIntegration
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.integrations import IntegrationStatus
from htco_domain.jobs import Schedule

ORDER_SYNC_JOB = "etsy.order_sync"
DELIVERED = "order.delivered"
# One requested history run per shop and period (the dashboard's stale reads), and the Owner's
# manual sync at most once a minute.
SYNC_PERIOD = Schedule(ORDER_SYNC_JOB, timedelta(minutes=10))
MANUAL_PERIOD = Schedule(ORDER_SYNC_JOB, timedelta(minutes=1))
SYNC_REQUESTER = ActorRef(ActorKind.SYSTEM, "order-sync-request")
RECONCILE = "reconcile"
SWEEP = "sweep"
RECONCILE_WINDOW = timedelta(days=INCREMENTAL_DAYS)
# Reconciliation (slice 5.5, the owner's decision of 2026-10-24: webhooks are the primary path,
# reconciliation runs twice a day). An hourly tick, off unless named in HTCO_WORKER_SCHEDULES,
# enqueues the runs whose configured local time (HTCO_RECONCILIATION_SCHEDULE in
# HTCO_RECONCILIATION_TZ) falls in its hour:
# the day's last run is the full sweep, any earlier one a 30-day reconcile.
RECONCILIATION_TICK = Schedule("orders.reconciliation", timedelta(hours=1))
ORDER_SCHEDULES = {RECONCILIATION_TICK.kind: RECONCILIATION_TICK}
# 06:00 and 18:00 in the owner's time zone, whatever the season.
DEFAULT_RECONCILIATION_TIMES = "06:00,18:00"
DEFAULT_RECONCILIATION_TZ = "Asia/Jerusalem"
_MODES = {"stale": "history", "manual": "history", RECONCILE: RECONCILE, SWEEP: SWEEP}

_log = logging.getLogger(__name__)


class IntakeStore(OrderStore, Protocol):
    async def order_count(self, shop_id: uuid.UUID, etsy_shop_id: int) -> int: ...

    async def record_cursor(
        self,
        integration: ShopIntegration,
        kind: str,
        *,
        started_at: datetime,
        error: str | None,
    ) -> None: ...

    async def ingest_receipts(
        self,
        integration: ShopIntegration,
        receipts: tuple[OrderReceipt, ...],
        *,
        context: IngestContext,
        now: datetime,
    ) -> tuple[IngestPlan, ...]: ...


class IntakeShops(Protocol):
    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None: ...


class ShopBusyError(JobError):
    """Another run holds the shop's order lock: retried by the execution's policy."""


@dataclass
class OrderIntake:
    """The `etsy-order-intake` handler."""

    store: IntakeStore
    shops: IntakeShops
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        if ctx.shop_id is None:
            raise JobError("an order intake names its shop")
        integration = await self.shops.describe(ctx.shop_id)
        if (
            integration is None
            or integration.organization_id != ctx.organization_id
            or integration.status is not IntegrationStatus.CONNECTED
        ):
            return Completed({"skipped": "not_connected"})
        receipts = OrderHistoryRead(ctx, self.clock)
        receipt_id = ctx.payload.get("receipt_id")
        if isinstance(receipt_id, int) and not isinstance(receipt_id, bool):
            return await self._receipt(ctx, integration, receipts, receipt_id)
        mode = ctx.payload.get("mode")
        if mode == "history":
            return await self._history(ctx, integration, receipts)
        if mode in {RECONCILE, SWEEP}:
            return await self._reconcile(ctx, integration, receipts, sweep=mode == SWEEP)
        raise JobError("an order intake names a receipt or a mode")

    async def _reconcile(
        self,
        ctx: HandlerContext,
        integration: ShopIntegration,
        receipts: OrderHistoryRead,
        *,
        sweep: bool,
    ) -> HandlerOutcome:
        """Reconciliation (slice 5.5): every receipt changed in the last 30 days, compared with
        the store (drifts recorded) and ingested. The sweep also reads every paid, unshipped
        receipt with no period limit and compares Etsy's receipt count with the store's."""
        kind = SWEEP if sweep else RECONCILE
        async with self.store.lock(integration.shop_id) as held:
            if not held:
                # Never lost (Codex review of #75): the execution retries after its backoff.
                raise ShopBusyError("another order sync of the shop is running")
            started = self.clock()
            context = IngestContext(
                IngestSource.RECONCILIATION,
                correlation_id=ctx.correlation_id,
                execution_id=ctx.execution_id,
                detect_drift=True,
            )
            more = more_drifts = 0
            count = None
            try:
                seen, drifts = await self._pages(
                    integration, receipts, context, since=started - RECONCILE_WINDOW
                )
                if sweep:  # the full sweep: open orders of any age, and the count
                    more, more_drifts = await self._pages(
                        integration, receipts, context, open_orders=True
                    )
                    count = await self._count(integration, receipts)
            except Exception as error:
                await self.store.record_cursor(
                    integration, kind, started_at=started, error=type(error).__name__
                )
                raise
            await self.store.record_cursor(integration, kind, started_at=started, error=None)
        output: dict[str, JsonValue] = {
            "mode": kind,
            "receipts_read": seen + more,
            "drifts": drifts + more_drifts,
        }
        if count is not None:
            output |= count
        return Completed(output)

    async def _pages(
        self,
        integration: ShopIntegration,
        receipts: OrderHistoryRead,
        context: IngestContext,
        *,
        since: datetime | None = None,
        open_orders: bool = False,
    ) -> tuple[int, int]:
        seen = drifts = 0
        offset: int | None = 0
        while offset is not None:
            page = await self._page(receipts, since=since, offset=offset, open_orders=open_orders)
            plans = await self.store.ingest_receipts(
                integration, page.items, context=context, now=self.clock()
            )
            seen += len(page.items)
            drifts += sum(len(plan.drifts) for plan in plans)
            offset = page.next_offset
        return seen, drifts

    async def _page(
        self, receipts: OrderHistoryRead, *, since: datetime | None, offset: int, open_orders: bool
    ) -> Page[OrderReceipt]:
        """One page; a deferral (429, the daily budget) is waited out a few times."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return await receipts.page(
                    changed_since=since, offset=offset, open_orders=open_orders
                )
            except MarketplaceRateLimitedError as error:
                if attempt == RATE_LIMIT_RETRIES:
                    raise
                wait = error.retry_after_seconds or float(2**attempt)
                await self.sleep(min(MAX_RATE_LIMIT_WAIT, max(1.0, wait)))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _count(
        self, integration: ShopIntegration, receipts: OrderHistoryRead
    ) -> dict[str, JsonValue]:
        """Etsy's receipt count against the store's (the nightly sweep): a gap is reported."""
        page = await receipts.page(limit=1)
        stored = await self.store.order_count(integration.shop_id, integration.etsy_shop_id)
        return {"etsy_receipts": page.total_count, "orders_stored": stored}

    async def _receipt(
        self,
        ctx: HandlerContext,
        integration: ShopIntegration,
        receipts: OrderHistoryRead,
        receipt_id: int,
    ) -> HandlerOutcome:
        receipt = await receipts.receipt(receipt_id)
        delivered = frozenset({receipt_id}) if ctx.payload.get("event_type") == DELIVERED else None
        context = IngestContext(
            IngestSource.WEBHOOK,
            correlation_id=ctx.correlation_id,
            execution_id=ctx.execution_id,
            delivered=delivered or frozenset(),
        )
        [plan] = await self.store.ingest_receipts(
            integration, (receipt,), context=context, now=self.clock()
        )
        return Completed(
            {
                "receipt_id": receipt_id,
                "written": plan.write,
                "state": None if plan.state is None else plan.state.value,
            }
        )

    async def _history(
        self, ctx: HandlerContext, integration: ShopIntegration, receipts: OrderHistoryRead
    ) -> HandlerOutcome:
        sync = OrderSync(
            store=self.store,
            pages=lambda _integration: receipts,
            clock=self.clock,
            sleep=self.sleep,
            backfill_context=BACKFILL,
            sync_context=IngestContext(
                IngestSource.SYNC, correlation_id=ctx.correlation_id, execution_id=ctx.execution_id
            ),
        )
        if not await sync.run(integration):
            return Completed({"mode": "history", "skipped": "locked"})
        state = await self.store.state(integration.shop_id)
        return Completed(
            {"mode": "history", "orders_stored": 0 if state is None else state.orders_stored}
        )


# --- Asking for a history run (the API process) and starting it (the worker) -----------------


@dataclass
class QueuedSyncRequests:
    """`SyncRequests` on the job queue: the API process enqueues, never runs, the sync."""

    queue: JobQueue
    offload: Offload = run_inline

    async def request(self, integration: ShopIntegration, *, now: datetime, force: bool) -> bool:
        period = MANUAL_PERIOD if force else SYNC_PERIOD
        reason = "manual" if force else "stale"
        spec = JobSpec(
            kind=ORDER_SYNC_JOB,
            idempotency_key=f"{ORDER_SYNC_JOB}:{integration.shop_id}:{reason}:"
            f"{period.key_for(now).rsplit('@', 1)[-1]}",
            payload={"reason": reason},
            organization_id=integration.organization_id,
            shop_id=integration.shop_id,
            lock_key=LockKeys.shop_orders(integration.shop_id),
            correlation_id=f"order-sync:{integration.shop_id}"[:200],
        )
        await self.offload(lambda: self.queue.enqueue(spec, now=now))
        return True

    async def enqueue_receipts(
        self,
        integration: ShopIntegration,
        receipt_ids: tuple[int, ...],
        *,
        now: datetime,
        correlation_id: str,
    ) -> int:
        """ "Re-sync selected" (slice 5.7): one job per receipt, the same receipt once a minute,
        on the shop's orders lock; the worker re-reads each through Etsy Order Intake."""
        bucket = MANUAL_PERIOD.key_for(now).rsplit("@", 1)[-1]
        specs = [
            JobSpec(
                kind=ORDER_SYNC_JOB,
                idempotency_key=f"{ORDER_SYNC_JOB}:{integration.shop_id}:resync:{receipt_id}:{bucket}",
                payload={"reason": "resync", "receipt_id": receipt_id},
                organization_id=integration.organization_id,
                shop_id=integration.shop_id,
                lock_key=LockKeys.shop_orders(integration.shop_id),
                correlation_id=correlation_id[:200],
            )
            for receipt_id in receipt_ids
        ]

        def run() -> int:
            for spec in specs:
                self.queue.enqueue(spec, now=now)
            return len(specs)

        return int(await self.offload(run))


@dataclass
class EngineSyncTrigger:
    """The `etsy.order_sync` job: starts Etsy Order Intake's history run for the shop."""

    engine: TriggersAutomations
    offload: Offload = run_inline

    async def run(self, job: ClaimedJob) -> bool:
        if job.organization_id is None or job.shop_id is None:
            raise JobError("an order sync job names its shop and organization")
        receipt_id = job.payload.get("receipt_id")
        if isinstance(receipt_id, int) and not isinstance(receipt_id, bool):
            return await self._resync(job, receipt_id)
        raw = job.payload.get("reason")
        reason = raw if isinstance(raw, str) and raw in _MODES else "stale"
        mode = _MODES[reason]
        scheduled = mode in {RECONCILE, SWEEP}
        request = TriggerRequest(
            automation_id=ORDER_INTAKE,
            trigger=TriggerKind.SCHEDULE if scheduled else TriggerKind.EVENT,
            actor=SYNC_REQUESTER,
            idempotency_key=f"etsy:{job.shop_id}:{mode}:{job.idempotency_key[-40:]}",
            payload={"mode": mode, "reason": reason},
            correlation_id=job.correlation_id or f"job:{job.id}",
            organization_id=job.organization_id,
            shop_id=job.shop_id,
            causation_id=f"job:{job.id}",
        )
        try:
            await self.offload(lambda: self.engine.trigger(request))
        except AutomationRefused:
            _log.warning("etsy.order_sync_not_started: shop=%s", job.shop_id)
            return False
        return True

    async def _resync(self, job: ClaimedJob, receipt_id: int) -> bool:
        """A selected receipt's re-read: Etsy Order Intake's receipt path, as for a webhook."""
        assert job.organization_id is not None  # noqa: S101 - checked by run
        assert job.shop_id is not None  # noqa: S101 - checked by run
        request = TriggerRequest(
            automation_id=ORDER_INTAKE,
            trigger=TriggerKind.EVENT,
            actor=SYNC_REQUESTER,
            idempotency_key=f"etsy:{job.shop_id}:{receipt_id}:resync:{job.idempotency_key[-24:]}",
            payload={"receipt_id": receipt_id, "event_type": "resync"},
            correlation_id=job.correlation_id or f"job:{job.id}",
            organization_id=job.organization_id,
            shop_id=job.shop_id,
            causation_id=f"job:{job.id}",
        )
        try:
            await self.offload(lambda: self.engine.trigger(request))
        except AutomationRefused:
            _log.warning("etsy.order_resync_not_started: shop=%s", job.shop_id)
            return False
        return True


def parse_reconciliation_times(text: str) -> tuple[time, ...]:
    """`"HH:MM,HH:MM"` (local times) into the day's run times, sorted; raises `ValueError`."""
    times: list[time] = []
    for part in text.split(","):
        raw = part.strip()
        hours, _, minutes = raw.partition(":")
        if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2):  # noqa: PLR2004
            raise ValueError(f"reconciliation time {raw!r} is not HH:MM")
        try:
            times.append(time(int(hours), int(minutes)))
        except ValueError:
            raise ValueError(f"reconciliation time {raw!r} is not a time of day") from None
    if len(set(times)) != len(times):
        raise ValueError("reconciliation times repeat")
    return tuple(sorted(times))


@dataclass(frozen=True)
class DueRun:
    reason: str  # RECONCILE or SWEEP
    slot: str  # the local slot, for example "2026-10-24T06:00+03:00": one run per shop and slot
    at: datetime  # when it runs (UTC)


def due_reconciliations(
    times: tuple[time, ...], *, now: datetime, tz: tzinfo
) -> tuple[DueRun, ...]:
    """Every run whose local time (in `tz`, so 06:00 stays 06:00 across a DST change) falls in
    the tick's hour, in order: the day's last time is the full sweep, any earlier one a
    reconcile. Each runs at its own time, not the tick's (Codex review of #75)."""
    hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end = hour + RECONCILIATION_TICK.every
    days = {hour.astimezone(tz).date(), (end - timedelta(microseconds=1)).astimezone(tz).date()}
    due = []
    for day in sorted(days):
        for at in times:
            moment = datetime.combine(day, at, tzinfo=tz)
            if hour <= moment.astimezone(UTC) < end:
                reason = SWEEP if at == times[-1] else RECONCILE
                due.append(
                    DueRun(reason, moment.isoformat(timespec="minutes"), moment.astimezone(UTC))
                )
    return tuple(due)


def enqueue_reconciliations(  # noqa: PLR0913 - the shops, the queue and the run
    shops: ConnectedShops,
    queue: JobQueue,
    *,
    reason: str,
    slot: str,
    now: datetime,
    run_at: datetime,
) -> int:
    """One run's fan-out: one `etsy.order_sync` job per connected shop and slot, under the
    shop's orders lock (`shop:{shop_id}:orders`)."""
    return fan_out_per_shop(
        queue,
        shops.connected_shops(),
        kind=ORDER_SYNC_JOB,
        key=lambda shop: f"{ORDER_SYNC_JOB}:{shop.shop_id}:{reason}:{slot}",
        lock=LockKeys.shop_orders,
        payload={"reason": reason},
        run_at=run_at,
        correlation=lambda shop: f"order-{reason}:{shop.shop_id}:{slot}"[:200],
        now=now,
    )


def daily_etsy_calls(
    *, shops: int, pages_30_days: int, open_pages: int, runs_per_day: int = 2
) -> int:
    """Etsy calls a day that reconciliation makes (AC-5.5.3): calls per run times runs per day.
    Each reconcile reads the 30-day pages; the day's sweep also the open orders' pages and one
    count read."""
    reconciles = max(0, runs_per_day - 1)
    sweep = pages_30_days + open_pages + 1
    return shops * (reconciles * pages_30_days + sweep)
