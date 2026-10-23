"""Ship-by and stock alerts (slice 6.9) on the one notification path of 6.4 (audit P2-2)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from htco_application.alerts import StockAlerts
from htco_application.automation.durable import Completed, HandlerContext
from htco_application.execution.gateway import UnavailableGateway
from htco_application.fulfillment_schedule import INCIDENT_DETECTION
from htco_application.incidents import EVENTS, IncidentService
from htco_application.messaging import OutboxMessage
from htco_application.notifications import NotificationService
from htco_application.sla_inspector import AwaitingOrder, IncidentDetection
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import BusinessCalendar, SlaPolicy, WindowUnit
from htco_domain.notifications import NotificationKind
from tests.fulfillment_fakes import MemoryIncidentStore, MemorySlaStore, MemoryTrackingStore
from tests.notification_fakes import Member, MemoryNotificationStore

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-0000000aa001")
SHOP = uuid.UUID("00000000-0000-0000-0000-0000000aa002")
OWNER = uuid.UUID("00000000-0000-0000-0000-0000000aa0a1")
PAID = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)  # a Monday
SHIP_BY = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # a Tuesday
LONG = SlaPolicy(id=uuid.UUID(int=9), window=30, unit=WindowUnit.CALENDAR_DAYS)


def run(
    now: datetime, *, calendar: BusinessCalendar | None = None, ship_by: datetime | None = SHIP_BY
):
    policy = (
        LONG
        if calendar is None
        else SlaPolicy(
            id=uuid.UUID(int=10), window=30, unit=WindowUnit.CALENDAR_DAYS, calendar=calendar
        )
    )
    sla = MemorySlaStore(MemoryTrackingStore(), policy_rows=[policy])
    sla.awaiting[SHOP] = [AwaitingOrder(51, PAID, expected_ship=ship_by)]
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    handler = IncidentDetection(
        sla,
        IncidentService(MemoryIncidentStore(), clock=lambda: now),
        clock=lambda: now,
        alerts=NotificationService(notes, clock=lambda: now),
    )
    return handler, notes


def context() -> HandlerContext:
    return HandlerContext(
        execution_id=uuid.uuid7(), automation_id=INCIDENT_DETECTION,
        actor=ActorRef(ActorKind.SYSTEM, "scheduler"), organization_id=ORG, shop_id=SHOP,
        correlation_id="sched:1", causation_id=None, attempt=1, payload={"mode": "inspect"},
        gateway=UnavailableGateway(),
    )  # fmt: skip


@pytest.mark.ac("AC-6.9.1")
async def test_ac_6_9_1_inside_the_warning_window_one_alert_once() -> None:
    handler, notes = run(datetime(2026, 10, 26, 9, 0, tzinfo=UTC))  # Monday: one business day
    first = await handler(context())
    await handler(context())

    assert isinstance(first, Completed)
    assert first.output["ship_by_alerts"] == 1
    [row] = notes.rows.values()
    assert row.kind is NotificationKind.ALERT_SHIP_BY
    assert row.params == {"receipt_id": "51", "ship_by": "2026-10-27"}
    assert len(notes.outbox) == 1  # one email


@pytest.mark.ac("AC-6.9.1")
async def test_ac_6_9_1_before_the_window_no_alert_and_holidays_are_skipped() -> None:
    friday = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    plain, plain_notes = run(friday)
    await plain(context())
    assert plain_notes.rows == {}
    # Monday the 26th a holiday in the shop's calendar: the window opens on Friday.
    holiday = BusinessCalendar(holidays=frozenset({date(2026, 10, 26)}))
    early, early_notes = run(friday, calendar=holiday)
    await early(context())
    assert len(early_notes.rows) == 1


async def test_an_order_without_a_ship_by_date_raises_nothing() -> None:
    handler, notes = run(SHIP_BY + timedelta(days=1), ship_by=None)
    await handler(context())
    assert notes.rows == {}


async def test_without_the_alert_service_the_inspector_still_runs() -> None:
    sla = MemorySlaStore(MemoryTrackingStore(), policy_rows=[LONG])
    sla.awaiting[SHOP] = [AwaitingOrder(51, PAID, expected_ship=SHIP_BY)]
    handler = IncidentDetection(
        sla, IncidentService(MemoryIncidentStore(), clock=lambda: SHIP_BY), clock=lambda: SHIP_BY
    )
    outcome = await handler(context())
    assert isinstance(outcome, Completed)
    assert outcome.output["ship_by_alerts"] == 0


def out_of_stock(listing: int = 777, at: str = "2026-10-25T08:00:00+00:00") -> OutboxMessage:
    payload: dict[str, Any] = {
        "event_type": "catalog.out_of_stock.v1", "listing_id": listing, "in_stock": False,
        "etsy_updated_at": at,
    }  # fmt: skip
    return OutboxMessage(ORG, EVENTS, f"catalog.out_of_stock.v1:{listing}:{at}", payload, SHOP)


async def test_stock_out_of_stock_alerts_once_per_listing_change() -> None:
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    alerts = StockAlerts(NotificationService(notes, clock=lambda: PAID))

    await alerts.handle(out_of_stock())
    await alerts.handle(out_of_stock())
    await alerts.handle(out_of_stock(at="2026-10-26T08:00:00+00:00"))

    assert len(notes.rows) == 2
    assert {r.kind for r in notes.rows.values()} == {NotificationKind.ALERT_OUT_OF_STOCK}
    assert {r.params["listing_id"] for r in notes.rows.values()} == {"777"}
    assert notes.outbox == {}  # in-app only by default


async def test_a_stock_event_without_a_listing_is_ignored() -> None:
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    message = OutboxMessage(ORG, EVENTS, "x", {"event_type": "catalog.out_of_stock.v1"}, SHOP)
    await StockAlerts(NotificationService(notes, clock=lambda: PAID)).handle(message)
    assert notes.rows == {}
