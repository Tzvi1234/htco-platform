"""Low-review alerts (slice 6.9, AC-6.9.2): Incident Detection & Escalation's `reviews` mode.

Every hour per shop, it reads the reviews written in the last `REVIEW_LOOKBACK` through the Action
Gateway (`etsy.reviews.read`, R0, the GET-only API channel) and raises one alert per review of
`LOW_REVIEW_STARS` or fewer (the owner's decision of 2026-10-23) on the one notification path of
6.4 (audit P2-2). The alert names the stars, the listing and, through the stored order items, the
order. The key is the review's transaction, so a review read again in the next hour alerts once.
The review's text (the buyer's words) never reaches HTCO's tables: the channel leaves it out.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from htco_application.automation.durable import HandlerContext
from htco_application.execution.gateway import ActionOutcome, ActionRef, ActionRequest
from htco_application.jobs import JobError, JsonValue
from htco_application.marketplace import MarketplaceRateLimitedError
from htco_application.notifications import NotificationRequest, NotificationService
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.notifications import NotificationKind

REVIEWS_READ = ActionRef("etsy.reviews.read", 1)
LOW_REVIEW_STARS = 3
# Twice the hourly period and more: a missed run or a slow Etsy still sees each review.
REVIEW_LOOKBACK = timedelta(hours=48)
PAGE = 100
# Pages until the window's reviews run out (Codex review of #108). The bound (SECURITY GATE item
# 9) is the app's whole daily Etsy budget (5000 calls, `etsy:app`): no run could read further.
MAX_PAGES = 50
# A deferral this short (the per-second budget) is waited out inside the scan, a few times.
MAX_INLINE_WAIT = 10.0
MAX_INLINE_WAITS = 10

type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]


class _DeferredError(Exception):
    """The gateway deferred a read: wait `wait` seconds (its `retry_at`)."""

    def __init__(self, wait: float, code: str) -> None:
        super().__init__(f"deferred for {wait:.0f} s ({code})")
        self.wait = wait
        self.code = code


class ReviewOrders(Protocol):
    def receipts_for_transactions(
        self, shop_id: uuid.UUID, transaction_ids: tuple[int, ...]
    ) -> Mapping[int, int]:
        """The stored order of each transaction, in this shop only; unknown ones are left out."""
        ...


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def low_review_alerts(  # noqa: PLR0913 - the execution and its collaborators
    ctx: HandlerContext,
    shop_id: uuid.UUID,
    *,
    orders: ReviewOrders,
    alerts: NotificationService,
    offload: Offload,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = _sleep,
) -> dict[str, JsonValue]:
    """Page by page, each page's low reviews alerted as it arrives (Codex review of #108): a
    deferral never throws away what was read. A short one (the shared per-second budget) is
    waited out inside the scan and the same page read again; a long one (the daily budget) ends
    the run with the gateway's retry time, and the retry's re-read adds no second alert."""
    start = clock() - REVIEW_LOOKBACK
    since = int(start.timestamp())
    read = raised = waits = 0
    truncated = False
    page = 0
    while True:
        if page == MAX_PAGES:
            truncated = True  # said in the output, never silent
            break
        try:
            output = await _read(ctx, shop_id, since=since, offset=page * PAGE, clock=clock)
        except _DeferredError as deferred:
            if deferred.wait <= MAX_INLINE_WAIT and waits < MAX_INLINE_WAITS:
                waits += 1
                await sleep(deferred.wait)
                continue  # the same page again
            raise MarketplaceRateLimitedError(
                f"etsy.reviews.read deferred ({deferred.code})", retry_after_seconds=deferred.wait
            ) from None
        found = output.get("reviews")
        items = [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []
        read += len(items)
        raised += await _alert(ctx, shop_id, items, orders=orders, alerts=alerts, offload=offload)
        count = output.get("count")
        if len(items) < PAGE or not isinstance(count, int) or count <= (page + 1) * PAGE:
            break
        if _older_than(items[-1], start):  # newest first: the window's edge is reached
            break
        page += 1
    result: dict[str, JsonValue] = {
        "mode": "reviews",
        "reviews_read": read,
        "low_review_alerts": raised,
    }
    if truncated:
        result["truncated"] = True
    return result


async def _alert(  # noqa: PLR0913 - one page and its collaborators
    ctx: HandlerContext,
    shop_id: uuid.UUID,
    reviews: Sequence[Mapping[str, JsonValue]],
    *,
    orders: ReviewOrders,
    alerts: NotificationService,
    offload: Offload,
) -> int:
    low = [r for r in reviews if _low(r)]
    transactions = tuple(t for r in low if isinstance(t := r.get("transaction_id"), int))
    linked: Mapping[int, int] = (
        await offload(lambda: orders.receipts_for_transactions(shop_id, transactions))
        if transactions
        else {}
    )
    raised = 0
    for review in low:
        transaction = review.get("transaction_id")
        params: dict[str, object] = {"rating": review["rating"], "listing_id": review["listing_id"]}
        if isinstance(transaction, int) and transaction in linked:
            params["receipt_id"] = linked[transaction]
        written = str(review.get("created_at") or "")[:19]
        key = (
            f"low_review:{shop_id}:{transaction}"
            if isinstance(transaction, int)
            else f"low_review:{shop_id}:{review['listing_id']}:{written}"
        )
        raised += (
            await alerts.notify(
                NotificationRequest(
                    organization_id=ctx.organization_id,
                    kind=NotificationKind.ALERT_LOW_REVIEW,
                    dedup_key=key[:120],
                    params=params,
                    shop_id=shop_id,
                    correlation_id=ctx.correlation_id,
                )
            )
            > 0
        )
    return raised


def _older_than(review: Mapping[str, JsonValue], start: datetime) -> bool:
    try:
        written = datetime.fromisoformat(str(review.get("created_at")))
    except ValueError:
        return False
    return written.tzinfo is not None and written < start


def _low(review: Mapping[str, JsonValue]) -> bool:
    """A readable review of `LOW_REVIEW_STARS` or fewer; anything else is skipped."""
    rating, listing = review.get("rating"), review.get("listing_id")
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:  # noqa: PLR2004 - stars
        return False
    if not isinstance(listing, int) or isinstance(listing, bool) or listing < 1:
        return False
    return rating <= LOW_REVIEW_STARS


async def _read(
    ctx: HandlerContext,
    shop_id: uuid.UUID,
    *,
    since: int,
    offset: int,
    clock: Callable[[], datetime],
) -> Mapping[str, JsonValue]:
    key = f"{ctx.execution_id}:{ctx.attempt}:reviews:{offset}"
    result = await ctx.gateway.submit(
        ActionRequest(
            action=REVIEWS_READ,
            actor=ActorRef(ActorKind.AUTOMATION, ctx.automation_id),
            organization_id=ctx.organization_id,
            shop_id=shop_id,
            idempotency_key=key[:200],
            correlation_id=ctx.correlation_id,
            causation_id=f"execution:{ctx.execution_id}",
            input={"min_created": since, "limit": PAGE, "offset": offset},
        )
    )
    if result.outcome is ActionOutcome.SUCCEEDED and result.output is not None:
        return result.output
    if result.outcome is ActionOutcome.DEFERRED:
        wait = 1.0
        if result.retry_at is not None:
            wait = max(1.0, (result.retry_at - clock()).total_seconds())
        raise _DeferredError(wait, result.error_code or "deferred")
    raise JobError(f"etsy.reviews.read {result.outcome.value}: {result.error_code or 'no output'}")
