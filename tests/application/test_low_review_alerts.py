"""Low-review alerts (slice 6.9, AC-6.9.2) on the one notification path of 6.4 (audit P2-2).

Incident Detection & Escalation's `reviews` mode reads the shop's newest reviews through the
Action Gateway (`etsy.reviews.read`, R0, GET only) and raises one alert per review of 3 stars or
fewer, linked to its listing and, through the stored order items, to its order.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    UnavailableGateway,
)
from htco_application.fulfillment_schedule import (
    INCIDENT_DETECTION,
    REVIEW_SCHEDULE,
    REVIEW_SHOP_JOB,
    ReviewCheck,
    enqueue_review_checks,
)
from htco_application.incidents import IncidentService
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob, JobError, JsonValue
from htco_application.maintenance import TenantShop
from htco_application.marketplace import MarketplaceRateLimitedError
from htco_application.notifications import NotificationService, render_email
from htco_application.review_alerts import REVIEW_LOOKBACK, REVIEWS_READ
from htco_application.sla_inspector import IncidentDetection
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.notifications import Locale, NotificationKind
from tests.application.test_tracking_sync import Engine
from tests.fulfillment_fakes import MemoryIncidentStore, MemorySlaStore, MemoryTrackingStore
from tests.job_fakes import MemoryJobQueue
from tests.notification_fakes import Member, MemoryNotificationStore

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-0000000ab001")
SHOP = uuid.UUID("00000000-0000-0000-0000-0000000ab002")
OWNER = uuid.UUID("00000000-0000-0000-0000-0000000ab0a1")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def review(transaction: int, rating: int, *, listing: int = 700) -> dict[str, JsonValue]:
    return {
        "transaction_id": transaction,
        "listing_id": listing,
        "rating": rating,
        "created_at": (NOW - timedelta(hours=2)).isoformat(),
    }


def succeeded(*reviews: dict[str, JsonValue]) -> ActionResult:
    return ActionResult(
        execution_id=uuid.uuid7(),
        outcome=ActionOutcome.SUCCEEDED,
        attempts=1,
        output={"reviews": list(reviews), "count": len(reviews)},
    )


@dataclass
class ScriptedGateway:
    results: list[ActionResult]
    requests: list[ActionRequest] = field(default_factory=list)

    async def submit(self, request: ActionRequest) -> ActionResult:
        self.requests.append(request)
        return self.results.pop(0)


def handler(
    orders: dict[int, int] | None = None,
) -> tuple[IncidentDetection, MemoryNotificationStore]:
    sla = MemorySlaStore(MemoryTrackingStore())
    sla.transactions[SHOP] = dict(orders or {})
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    detection = IncidentDetection(
        sla,
        IncidentService(MemoryIncidentStore(), clock=lambda: NOW),
        clock=lambda: NOW,
        alerts=NotificationService(notes, clock=lambda: NOW),
    )
    return detection, notes


def context(gateway: Any, *, shop: uuid.UUID | None = SHOP) -> HandlerContext:
    return HandlerContext(
        execution_id=uuid.uuid7(), automation_id=INCIDENT_DETECTION,
        actor=ActorRef(ActorKind.SYSTEM, "scheduler"), organization_id=ORG, shop_id=shop,
        correlation_id="sched:reviews", causation_id=None, attempt=1,
        payload={"mode": "reviews"}, gateway=gateway,
    )  # fmt: skip


@pytest.mark.ac("AC-6.9.2")
async def test_ac_6_9_2_a_review_of_3_stars_or_fewer_alerts_once_with_listing_and_order() -> None:
    detection, notes = handler({9001: 51, 9002: 52, 9003: 53})
    reviews = (review(9001, 2, listing=701), review(9002, 5), review(9003, 3, listing=703))
    gateway = ScriptedGateway([succeeded(*reviews), succeeded(*reviews)])

    first = await detection(context(gateway))
    await detection(context(gateway))  # the next hour sees the same reviews

    assert isinstance(first, Completed)
    assert first.output == {"mode": "reviews", "reviews_read": 3, "low_review_alerts": 2}
    rows = sorted(notes.rows.values(), key=lambda r: r.params["receipt_id"])
    assert [r.kind for r in rows] == [NotificationKind.ALERT_LOW_REVIEW] * 2
    assert [r.params for r in rows] == [
        {"rating": "2", "listing_id": "701", "receipt_id": "51"},
        {"rating": "3", "listing_id": "703", "receipt_id": "53"},
    ]
    assert len(notes.outbox) == 2  # one email each, never a second
    email = render_email(rows[0].kind, Locale.EN, rows[0].params, link="https://x.test/")
    assert "listing 701, order 51" in email.text


@pytest.mark.ac("AC-6.9.2")
async def test_ac_6_9_2_the_read_goes_through_the_gateway_as_the_automation() -> None:
    detection, _ = handler()
    gateway = ScriptedGateway([succeeded()])

    await detection(context(gateway))

    [request] = gateway.requests
    assert request.action == REVIEWS_READ
    assert REVIEWS_READ.name == "etsy.reviews.read"
    assert request.actor == ActorRef(ActorKind.AUTOMATION, INCIDENT_DETECTION)
    assert request.shop_id == SHOP
    assert request.input["min_created"] == int((NOW - REVIEW_LOOKBACK).timestamp())


async def test_a_review_of_an_order_not_stored_still_alerts_with_its_listing() -> None:
    detection, notes = handler()
    gateway = ScriptedGateway([succeeded(review(9100, 1, listing=710))])

    await detection(context(gateway))

    [row] = notes.rows.values()
    assert row.params == {"rating": "1", "listing_id": "710"}
    email = render_email(row.kind, Locale.EN, row.params, link="https://x.test/")
    assert email.subject == "A 1-star review"
    assert "listing 710" in email.text


async def test_a_deferred_read_raises_a_rate_limit_with_the_wait() -> None:
    detection, notes = handler()
    deferred = ActionResult(
        execution_id=uuid.uuid7(), outcome=ActionOutcome.DEFERRED, attempts=0,
        error_code="rate_limited", retry_at=NOW + timedelta(seconds=9),
    )  # fmt: skip

    with pytest.raises(MarketplaceRateLimitedError) as raised:
        await detection(context(ScriptedGateway([deferred])))
    assert raised.value.retry_after_seconds == 9
    assert notes.rows == {}


async def test_a_denied_read_fails_the_run_with_a_safe_message() -> None:
    detection, _ = handler()
    denied = ActionResult(
        execution_id=uuid.uuid7(), outcome=ActionOutcome.DENIED, attempts=0,
        error_code="forbidden",
    )  # fmt: skip

    with pytest.raises(JobError, match="denied: forbidden"):
        await detection(context(ScriptedGateway([denied])))


async def test_an_unreadable_review_is_skipped_not_alerted() -> None:
    detection, notes = handler()
    broken: dict[str, JsonValue] = {"transaction_id": 9200, "listing_id": 720, "rating": 9}
    gateway = ScriptedGateway([succeeded(broken, review(9201, 2))])

    outcome = await detection(context(gateway))

    assert isinstance(outcome, Completed)
    assert outcome.output["low_review_alerts"] == 1
    assert len(notes.rows) == 1


async def test_the_reviews_mode_names_its_shop() -> None:
    detection, _ = handler()
    with pytest.raises(JobError, match="names its shop"):
        await detection(context(UnavailableGateway(), shop=None))


async def test_without_the_alert_service_no_review_is_read() -> None:
    sla = MemorySlaStore(MemoryTrackingStore())
    detection = IncidentDetection(
        sla, IncidentService(MemoryIncidentStore(), clock=lambda: NOW), clock=lambda: NOW
    )
    gateway = ScriptedGateway([])

    outcome = await detection(context(gateway))

    assert outcome == Completed({"mode": "reviews", "reviews_read": 0, "low_review_alerts": 0})
    assert gateway.requests == []


# --- The hourly schedule ----------------------------------------------------------------------


class Shops:
    def connected_shops(self) -> list[TenantShop]:
        return [TenantShop(ORG, SHOP)]


def test_the_review_schedule_is_hourly_one_job_per_shop_and_hour() -> None:
    queue = MemoryJobQueue()

    assert enqueue_review_checks(Shops(), queue, now=NOW) == 1
    enqueue_review_checks(Shops(), queue, now=NOW + timedelta(minutes=20))

    jobs = [j for j in queue.jobs.values() if j.spec.kind == REVIEW_SHOP_JOB]
    assert len(jobs) == 1
    assert REVIEW_SCHEDULE.every == timedelta(hours=1)


def review_job(payload: dict[str, Any], *, shop: uuid.UUID | None = SHOP) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid7(), kind=REVIEW_SHOP_JOB, idempotency_key="k", payload=payload,
        organization_id=ORG, shop_id=shop, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW,
    )  # fmt: skip


def test_the_shop_job_starts_incident_detection_in_the_reviews_mode() -> None:
    engine = Engine()

    started = ReviewCheck(engine).run(review_job({"period": "2026-10-25T09:00Z"}))

    assert started is True
    [request] = engine.requests
    assert request.automation_id == INCIDENT_DETECTION
    assert request.idempotency_key == f"{INCIDENT_DETECTION}:reviews:{SHOP}:2026-10-25T09:00Z"
    assert (request.payload, request.trigger) == ({"mode": "reviews"}, TriggerKind.SCHEDULE)


def test_a_draft_automation_is_refused_without_failing_the_review_job() -> None:
    assert ReviewCheck(Engine(refuse=True)).run(review_job({"period": "p"})) is False
    with pytest.raises(JobError, match="period"):
        ReviewCheck(Engine()).run(review_job({}))
    with pytest.raises(JobError, match="shop and organization"):
        ReviewCheck(Engine()).run(review_job({"period": "p"}, shop=None))
