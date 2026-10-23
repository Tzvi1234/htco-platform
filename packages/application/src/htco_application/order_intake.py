"""Etsy Order Intake on the durable engine (slice 5.4): the `etsy-order-intake` handler.

Every Etsy read goes through the Action Gateway (`etsy.receipts.read`, API channel, the guarded
GET-only adapter, the app-wide rate budget and the `etsy:api` breaker), in its order history form:
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import ORDER_INTAKE, TriggersAutomations
from htco_application.execution.gateway import ActionOutcome, ActionRef, ActionRequest
from htco_application.jobs import ClaimedJob, JobError, JobQueue, JobSpec, JsonValue
from htco_application.marketplace import (
    AccessTokenProvider,
    MarketplaceRateLimitedError,
    OrderReceipt,
    Page,
)
from htco_application.order_history import PAGE, OrderStore, OrderSync
from htco_application.order_ingest import (
    BACKFILL,
    IngestContext,
    IngestPlan,
    IngestSource,
)
from htco_application.order_receipts import receipt_from_json
from htco_application.shop_integrations import ShopIntegration
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.integrations import IntegrationStatus
from htco_domain.jobs import Schedule

RECEIPTS_READ = ActionRef("etsy.receipts.read", 1)
ORDER_SYNC_JOB = "etsy.order_sync"
DELIVERED = "order.delivered"
# One requested history run per shop and period (the dashboard's stale reads), and the Owner's
# manual sync at most once a minute.
SYNC_PERIOD = Schedule(ORDER_SYNC_JOB, timedelta(minutes=10))
MANUAL_PERIOD = Schedule(ORDER_SYNC_JOB, timedelta(minutes=1))
SYNC_REQUESTER = ActorRef(ActorKind.SYSTEM, "order-sync-request")

_log = logging.getLogger(__name__)


class IntakeStore(OrderStore, Protocol):
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


class IntakeActionError(JobError):
    """The gateway refused or failed the read: the execution retries by its policy."""


class _NoPinning:
    """The gateway's API channel reads with the vault's own token source; nothing to pin."""

    def pinned(self, shop_id: uuid.UUID, *, generation: int) -> AccessTokenProvider:  # noqa: ARG002 - the port
        return _UNUSED


class _UnusedTokens:
    async def access_token(self, shop_id: int) -> str:  # pragma: no cover - never called
        del shop_id
        raise AssertionError("the gateway reads with its own tokens")


_UNUSED = _UnusedTokens()


@dataclass
class GatewayReceipts:
    """The order history's `ReceiptSource` through the Action Gateway, for one execution."""

    context: HandlerContext
    clock: Callable[[], datetime]

    async def list_order_receipts(  # noqa: PLR0913 - the ReceiptSource port
        self,
        shop_id: int,  # noqa: ARG002 - the gateway resolves the shop's Etsy ID itself
        *,
        min_created: datetime | None,  # noqa: ARG002 - the order history reads all, or changes
        min_last_modified: datetime | None = None,
        limit: int = PAGE,
        offset: int = 0,
        urgent: bool = False,  # noqa: ARG002 - intake reads are routine
        tokens: AccessTokenProvider | None = None,  # noqa: ARG002 - see _NoPinning
    ) -> Page[OrderReceipt]:
        values: dict[str, JsonValue] = {"order_history": True, "limit": limit, "offset": offset}
        window = "all"
        if min_last_modified is not None:
            since = int(min_last_modified.timestamp())
            values["min_last_modified"] = since
            window = f"since{since}"
        output = await self._read(values, f"{window}:{offset}")
        receipts = tuple(_receipts(output))
        count = output.get("count")
        return Page(
            items=receipts,
            total_count=count if isinstance(count, int) else len(receipts),
            offset=offset,
        )

    async def get_order_receipt(self, receipt_id: int) -> OrderReceipt:
        output = await self._read(
            {"order_history": True, "receipt_id": receipt_id}, f"receipt:{receipt_id}"
        )
        found = [r for r in _receipts(output) if r.receipt_id == receipt_id]
        if not found:
            raise IntakeActionError(f"etsy.receipts.read returned no receipt {receipt_id}")
        return found[0]

    async def _read(self, values: dict[str, JsonValue], part: str) -> Mapping[str, JsonValue]:
        ctx = self.context
        # Unique per execution attempt and page: an R1 read's output is never stored, so a
        # finished key could not answer again; a deferred key continues after its retry_at.
        key = f"{ctx.execution_id}:{ctx.attempt}:{part}"
        result = await ctx.gateway.submit(
            ActionRequest(
                action=RECEIPTS_READ,
                actor=ActorRef(ActorKind.AUTOMATION, ctx.automation_id),
                organization_id=ctx.organization_id,
                shop_id=ctx.shop_id,
                idempotency_key=key[:200],
                correlation_id=ctx.correlation_id,
                causation_id=f"execution:{ctx.execution_id}",
                input=values,
            )
        )
        if result.outcome is ActionOutcome.SUCCEEDED and result.output is not None:
            return result.output
        if result.outcome is ActionOutcome.DEFERRED:
            wait = None
            if result.retry_at is not None:
                wait = max(1.0, (result.retry_at - self.clock()).total_seconds())
            # The order history waits out a 429 a few times, then the run fails into a retry.
            raise MarketplaceRateLimitedError(
                f"etsy.receipts.read deferred ({result.error_code})", retry_after_seconds=wait
            )
        raise IntakeActionError(
            f"etsy.receipts.read {result.outcome.value}: {result.error_code or 'no output'}"
        )


def _receipts(output: Mapping[str, JsonValue]) -> list[OrderReceipt]:
    raw = output.get("receipts")
    if not isinstance(raw, list):
        raise IntakeActionError("etsy.receipts.read returned no receipts list")
    try:
        return [receipt_from_json(item) for item in raw if isinstance(item, dict)]
    except KeyError, TypeError, ValueError:
        raise IntakeActionError("etsy.receipts.read returned an unreadable receipt") from None


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
        receipts = GatewayReceipts(ctx, self.clock)
        receipt_id = ctx.payload.get("receipt_id")
        if isinstance(receipt_id, int) and not isinstance(receipt_id, bool):
            return await self._receipt(ctx, integration, receipts, receipt_id)
        if ctx.payload.get("mode") == "history":
            return await self._history(ctx, integration, receipts)
        raise JobError("an order intake names a receipt or a mode")

    async def _receipt(
        self,
        ctx: HandlerContext,
        integration: ShopIntegration,
        receipts: GatewayReceipts,
        receipt_id: int,
    ) -> HandlerOutcome:
        receipt = await receipts.get_order_receipt(receipt_id)
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
        self, ctx: HandlerContext, integration: ShopIntegration, receipts: GatewayReceipts
    ) -> HandlerOutcome:
        sync = OrderSync(
            store=self.store,
            source=receipts,
            tokens=_NoPinning(),
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

type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]


async def _inline(work: Callable[[], Any]) -> Any:
    return work()


@dataclass
class QueuedSyncRequests:
    """`SyncRequests` on the job queue: the API process enqueues, never runs, the sync."""

    queue: JobQueue
    offload: Offload = _inline

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
            correlation_id=f"order-sync:{integration.shop_id}"[:200],
        )
        await self.offload(lambda: self.queue.enqueue(spec, now=now))
        return True


@dataclass
class EngineSyncTrigger:
    """The `etsy.order_sync` job: starts Etsy Order Intake's history run for the shop."""

    engine: TriggersAutomations
    offload: Offload = _inline

    async def run(self, job: ClaimedJob) -> bool:
        if job.organization_id is None or job.shop_id is None:
            raise JobError("an order sync job names its shop and organization")
        reason = job.payload.get("reason")
        request = TriggerRequest(
            automation_id=ORDER_INTAKE,
            trigger=TriggerKind.EVENT,
            actor=SYNC_REQUESTER,
            idempotency_key=f"etsy:{job.shop_id}:history:{job.idempotency_key[-40:]}",
            payload={"mode": "history", "reason": reason if isinstance(reason, str) else "stale"},
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
