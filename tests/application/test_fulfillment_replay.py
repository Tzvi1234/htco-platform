"""Slice 6.7's replay tests on the automation path: the same shipment observed again and again
through both of Tracking Synchronization's triggers (AC-6.7.2), and the same SLA breach inspected
again and again through Incident Detection & Escalation down to the email it sends (AC-6.7.3).
In-memory stores with Postgres's uniqueness rules; no Etsy and no email provider. All values are
invented."""

import uuid
from datetime import timedelta

import pytest

from htco_application.automation.durable import Completed
from htco_application.incidents import INCIDENT_OPENED, SLA_BREACHED, IncidentService
from htco_application.kernel import EVENTS_DESTINATION
from htco_application.messaging import OutboxMessage, PendingDelivery
from htco_application.notifications import (
    EMAIL_DESTINATION,
    EmailDeliverer,
    EventDeliverer,
    IncidentNotifications,
    NotificationService,
)
from htco_application.sla_inspector import IncidentDetection
from htco_application.tracking_sync import TRACKING_DETECTED
from htco_domain.incidents import Cause
from htco_domain.notifications import NotificationKind
from htco_domain.orders import OrderState
from tests.application.test_action_gateway import SHOP
from tests.application.test_execution_router import FakeMarketplace
from tests.application.test_order_intake import Intake
from tests.application.test_sla_inspector import PAID, Inspector
from tests.application.test_tracking_sync import RECEIPT, Sync
from tests.notification_fakes import (
    FakeEmail,
    Member,
    MemoryEmailDirectory,
    MemoryNotificationStore,
)
from tests.order_intake_fakes import paid_receipt, shipped

pytestmark = pytest.mark.anyio

REPLAYS = 6
OWNER = uuid.UUID("00000000-0000-0000-0000-00000000d0a1")


def _delivery(message: OutboxMessage) -> PendingDelivery:
    return PendingDelivery(id=uuid.uuid7(), message=message, attempts=0, delivered=False)


# --- AC-6.7.2: one shipment, both triggers, replayed -------------------------------------------


@pytest.mark.ac("AC-6.7.2")
async def test_ac_6_7_2_the_same_shipment_through_schedule_and_event_paths_is_recorded_once() -> (
    None
):
    """The webhook and the schedule interleave on one receipt, the schedule re-reading it as if
    Etsy had touched it each time; Etsy Order Intake sees `order.shipped` as often. One tracking
    event, one transition to `SHIPPED`, and nothing goes to Etsy: every outbox message is an
    event, every Etsy call is the receipt's GET."""
    marketplace = FakeMarketplace(order_receipts=(shipped(paid_receipt(RECEIPT), minutes=9),))
    sync = Sync(marketplace)
    intake = Intake(marketplace)
    sync.tracking.shipped[SHOP] = [RECEIPT]
    event = {"receipt_id": RECEIPT, "event_type": "order.shipped"}

    outcomes = []
    for attempt in range(1, REPLAYS + 1):
        outcomes.append(await sync.handler(sync.context(event, attempt=attempt)))
        sync.tracking.changed_on_etsy(SHOP, RECEIPT)
        outcomes.append(await sync.handler(sync.context({"mode": "schedule"}, attempt=attempt)))
        await intake.handler(intake.context(event, attempt=attempt))

    assert outcomes[0] == Completed({"receipts": 1, "recorded": 1, "via": "webhook"})
    assert outcomes[1] == Completed({"receipts": 1, "recorded": 0, "via": "schedule"})
    assert all(isinstance(o, Completed) and o.output["recorded"] == 0 for o in outcomes[1:])
    assert len(sync.tracking.rows) == 1
    assert len(sync.tracking.events.of(TRACKING_DETECTED)) == 1
    to_shipped = [t for (_, t, _) in intake.store.transitions if t.to_state is OrderState.SHIPPED]
    assert len(to_shipped) == 1
    outbox = [*sync.tracking.events.messages.values(), *intake.store.events.values()]
    assert {m.destination for m in outbox} == {EVENTS_DESTINATION}
    assert {call[0] for call in marketplace.calls} == {"get_order_receipt"}


# --- AC-6.7.3: one breach, replayed down to the email ------------------------------------------


@pytest.mark.ac("AC-6.7.3")
async def test_ac_6_7_3_the_same_breach_replayed_yields_one_incident_notification_and_email() -> (
    None
):
    """Each round inspects the breached order again and delivers every outbox message so far
    again (a replayed delivery), events to their subscribers and emails to the provider."""
    inspector = Inspector(PAID + timedelta(days=5))
    inspector.await_order(51)
    members = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    notifications = NotificationService(members, clock=lambda: inspector.now)
    handler = IncidentDetection(
        inspector.sla,
        IncidentService(inspector.incident_store, clock=lambda: inspector.now),
        clock=lambda: inspector.now,
        alerts=notifications,
    )
    events = EventDeliverer({INCIDENT_OPENED: IncidentNotifications(notifications)})
    email = FakeEmail()
    emails = EmailDeliverer(
        email, MemoryEmailDirectory({OWNER: "owner@example.test"}),
        clock=lambda: inspector.now, reply_to="support@example.test",
        office_url="https://office.example.test",
    )  # fmt: skip

    for _ in range(REPLAYS):
        await handler(inspector.context({"mode": "inspect"}))
        for message in list(inspector.incident_store.events.messages.values()):
            await events.deliver(_delivery(message))
        for message in list(members.outbox.values()):
            assert message.destination == EMAIL_DESTINATION
            await emails.deliver(_delivery(message))

    [incident] = inspector.incident_store.incidents.values()
    assert incident.cause is Cause.TRACKING_MISSING
    assert len(inspector.incident_store.events.of(SLA_BREACHED)) == 1
    assert len(inspector.incident_store.events.of(INCIDENT_OPENED)) == 1
    [notification] = members.rows.values()
    assert notification.kind is NotificationKind.INCIDENT_OPENED
    assert notification.user_id == OWNER
    [sent] = email.sent.values()
    [queued] = members.outbox.values()
    assert sent.idempotency_key == queued.idempotency_key
    assert str(incident.id) in queued.idempotency_key
