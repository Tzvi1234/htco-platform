"""The automations' typed clients for the Etsy reads through the Action Gateway (AUD-06, #147).

One client per action, each with its JSON codec beside its contract (`order_receipts`), so a
handler never builds an `ActionRequest` itself:

- `OrderHistoryRead` (`etsy.order_history.read`): the whole receipt as the order history stores
  it, buyer data included. Granted to Etsy Order Intake alone (`RESTRICTED_GRANTS`).
- `ShipmentsRead` (`etsy.shipments.read`): a receipt's shipments with their tracking codes, no
  buyer data. Granted to Tracking Sync alone.
- `ReceiptsRead` (`etsy.receipts.read`, its order history form): the receipt without buyer data
  and without tracking codes, for the other automations (Order Deduplication & Verification).

Each read is an R1 output the gateway returns and never stores.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from htco_application.automation.durable import HandlerContext
from htco_application.execution.gateway import ActionOutcome, ActionRef, ActionRequest
from htco_application.kernel import JobError, JsonValue, Page
from htco_application.marketplace import MarketplaceRateLimitedError, OrderReceipt, OrderShipment
from htco_application.order_history import PAGE
from htco_application.order_receipts import receipt_from_json, shipments_from_json
from htco_domain.actors import ActorKind, ActorRef

RECEIPTS_READ = ActionRef("etsy.receipts.read", 1)
ORDER_HISTORY_READ = ActionRef("etsy.order_history.read", 1)
SHIPMENTS_READ = ActionRef("etsy.shipments.read", 1)


class IntakeActionError(JobError):
    """The gateway refused or failed the read: the execution retries by its policy."""


@dataclass
class _GatewayRead:
    context: HandlerContext
    clock: Callable[[], datetime]

    async def _submit(
        self, action: ActionRef, values: dict[str, JsonValue], part: str
    ) -> Mapping[str, JsonValue]:
        ctx = self.context
        # Unique per execution attempt and page: an R1 read's output is never stored, so a
        # finished key could not answer again; a deferred key continues after its retry_at.
        key = f"{ctx.execution_id}:{ctx.attempt}:{part}"
        result = await ctx.gateway.submit(
            ActionRequest(
                action=action,
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
            # The caller waits out a 429 a few times, then the run fails into a retry.
            raise MarketplaceRateLimitedError(
                f"{action.name} deferred ({result.error_code})", retry_after_seconds=wait
            )
        raise IntakeActionError(
            f"{action.name} {result.outcome.value}: {result.error_code or 'no output'}"
        )

    async def _receipt(
        self, action: ActionRef, values: dict[str, JsonValue], receipt_id: int
    ) -> OrderReceipt:
        output = await self._submit(action, values, f"receipt:{receipt_id}")
        found = [r for r in _receipts(action, output) if r.receipt_id == receipt_id]
        if not found:
            raise IntakeActionError(f"{action.name} returned no receipt {receipt_id}")
        return found[0]


@dataclass
class OrderHistoryRead(_GatewayRead):
    """`etsy.order_history.read`, for one execution of Etsy Order Intake: the order history's
    `ReceiptPages` and the one receipt of a webhook."""

    async def page(
        self,
        *,
        changed_since: datetime | None = None,
        offset: int = 0,
        limit: int = PAGE,
        open_orders: bool = False,
    ) -> Page[OrderReceipt]:
        values: dict[str, JsonValue] = {"limit": limit, "offset": offset}
        window = "all"
        if open_orders:  # every paid, unshipped receipt, whenever it was ordered (5.5)
            values |= {"was_paid": True, "was_shipped": False}
            window = "open"
        if changed_since is not None:
            since = int(changed_since.timestamp())
            values["min_last_modified"] = since
            window = f"since{since}"
        output = await self._submit(ORDER_HISTORY_READ, values, f"{window}:{offset}:{limit}")
        receipts = tuple(_receipts(ORDER_HISTORY_READ, output))
        count = output.get("count")
        return Page(
            items=receipts,
            total_count=count if isinstance(count, int) else len(receipts),
            offset=offset,
        )

    async def receipt(self, receipt_id: int) -> OrderReceipt:
        return await self._receipt(ORDER_HISTORY_READ, {"receipt_id": receipt_id}, receipt_id)


@dataclass
class ReceiptsRead(_GatewayRead):
    """`etsy.receipts.read` in its order history form: the receipt without buyer data and
    without tracking codes."""

    async def receipt(self, receipt_id: int) -> OrderReceipt:
        values: dict[str, JsonValue] = {"order_history": True, "receipt_id": receipt_id}
        return await self._receipt(RECEIPTS_READ, values, receipt_id)


@dataclass
class ShipmentsRead(_GatewayRead):
    """`etsy.shipments.read`, for one execution of Tracking Sync: a receipt's shipments."""

    async def shipments(self, receipt_id: int) -> tuple[OrderShipment, ...]:
        output = await self._submit(
            SHIPMENTS_READ, {"receipt_id": receipt_id}, f"shipments:{receipt_id}"
        )
        raw = output.get("shipments")
        if not isinstance(raw, list):
            raise IntakeActionError(f"{SHIPMENTS_READ.name} returned no shipments list")
        try:
            found = [shipments_from_json(item) for item in raw if isinstance(item, dict)]
        except KeyError, TypeError, ValueError:
            raise IntakeActionError(
                f"{SHIPMENTS_READ.name} returned unreadable shipments"
            ) from None
        for found_id, shipments in found:
            if found_id == receipt_id:
                return shipments
        raise IntakeActionError(f"{SHIPMENTS_READ.name} returned no receipt {receipt_id}")


def _receipts(action: ActionRef, output: Mapping[str, JsonValue]) -> list[OrderReceipt]:
    raw = output.get("receipts")
    if not isinstance(raw, list):
        raise IntakeActionError(f"{action.name} returned no receipts list")
    try:
        return [receipt_from_json(item) for item in raw if isinstance(item, dict)]
    except KeyError, TypeError, ValueError:
        raise IntakeActionError(f"{action.name} returned an unreadable receipt") from None
