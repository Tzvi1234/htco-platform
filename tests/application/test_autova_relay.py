"""The AutoVA tracking relay (slice 6.10), its Human-task path until Phase 8A.

A paid order without tracking past the relay window (2 business days, ASSUMPTIONS A-20) opens
exactly one relay item; the owner pastes the number from AutoVA, which is validated before
anything happens; the item is handed to a person to copy into Etsy (no Etsy write here, 6.8 adds
the guarded upload); tracking that reaches the Etsy receipt closes it, and a number is never
used twice.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.notifications import NotificationService
from htco_application.relay import (
    DuplicateTrackingError,
    RelayItemMovedError,
    RelayItemNotVisibleError,
    list_relay_items,
    open_due_relay_items,
    submit_tracking,
)
from htco_application.sla_inspector import AwaitingOrder
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import TrackingFormatError, tracking_fingerprint
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.notifications import NotificationKind
from htco_domain.relay import ClosedReason, RelayState
from tests.notification_fakes import Member, MemoryNotificationStore
from tests.relay_fakes import MemoryRelayStore

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-0000000ac001")
OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000ac0ff")
SHOP = uuid.UUID("00000000-0000-0000-0000-0000000ac002")
OTHER_SHOP = uuid.UUID("00000000-0000-0000-0000-0000000ac003")
OWNER = uuid.UUID("00000000-0000-0000-0000-0000000ac0a1")
PAID = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)  # a Monday
CODE = "9400100000000000001234"  # a USPS-shaped test number
RELAY_ACTOR = ActorRef(ActorKind.AUTOMATION, "incident-detection-escalation")


def auth(
    role: Role = Role.OWNER, *, shops: frozenset[uuid.UUID] = frozenset({SHOP})
) -> AuthContext:
    return AuthContext(
        user_id=OWNER,
        assurance=AssuranceLevel.AAL2,
        session_id=None,
        memberships=(Membership(ORG, role, shops),),
    )


async def opened(
    store: MemoryRelayStore, now: datetime, *, orders: tuple[AwaitingOrder, ...] | None = None
) -> tuple[int, MemoryNotificationStore]:
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    count = await open_due_relay_items(
        store,
        NotificationService(notes, clock=lambda: now),
        organization_id=ORG,
        shop_id=SHOP,
        awaiting=orders if orders is not None else (AwaitingOrder(51, PAID),),
        now=now,
        correlation_id="sched:relay",
    )
    return count, notes


# --- Detect (AC-6.10.1) ----------------------------------------------------------------------


@pytest.mark.ac("AC-6.10.1")
async def test_ac_6_10_1_past_the_relay_window_one_item_and_one_notification() -> None:
    store = MemoryRelayStore()
    wednesday_noon = PAID + timedelta(days=2, minutes=1)  # 2 business days and a minute

    first, notes = await opened(store, wednesday_noon)
    again, _ = await opened(store, wednesday_noon + timedelta(minutes=15))

    assert (first, again) == (1, 0)
    [item] = store.items.values()
    assert (item.receipt_id, item.state) == (51, RelayState.AWAITING_TRACKING)
    [row] = notes.rows.values()
    assert row.kind is NotificationKind.RELAY_TRACKING_NEEDED
    assert row.params == {"receipt_id": "51", "relay_item_id": str(item.id)}


async def test_inside_the_relay_window_nothing_opens_and_weekends_do_not_count() -> None:
    store = MemoryRelayStore()
    friday = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    count, notes = await opened(
        store, friday + timedelta(days=3), orders=(AwaitingOrder(52, friday),)
    )  # Monday: one business day
    assert count == 0
    assert store.items == {}
    assert notes.rows == {}


# --- Paste and hand off (AC-6.10.1, AC-6.10.3, AC-6.10.4) -------------------------------------


async def an_item(store: MemoryRelayStore) -> uuid.UUID:
    await opened(store, PAID + timedelta(days=3))
    [item] = store.items.values()
    return item.id


@pytest.mark.ac("AC-6.10.4")
async def test_ac_6_10_4_an_invalid_number_is_refused_before_anything_happens() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)

    with pytest.raises(TrackingFormatError, match="not a valid usps tracking number"):
        submit_tracking(
            auth(), store, organization_id=ORG, item_id=item, carrier="usps", code="12345",
            now=PAID, correlation_id="req-1",
        )  # fmt: skip
    with pytest.raises(TrackingFormatError, match="unknown carrier"):
        submit_tracking(
            auth(), store, organization_id=ORG, item_id=item, carrier="pigeon", code=CODE,
            now=PAID, correlation_id="req-1",
        )  # fmt: skip

    assert store.items[item].state is RelayState.AWAITING_TRACKING
    assert store.codes == {}
    assert store.audits == []


@pytest.mark.ac("AC-6.10.3")
async def test_ac_6_10_3_a_pasted_number_is_handed_to_a_person_never_silently() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)

    moved = submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps",
        code="9400 1000 0000 0000 0012 34", now=PAID, correlation_id="req-1",
    )  # fmt: skip

    assert moved.state is RelayState.HANDED_OFF
    assert (moved.carrier, moved.last4) == ("usps", "1234")
    assert store.codes[item] == CODE  # normalised; kept only for the copy button
    [audit] = store.audits
    assert (audit.action, audit.entity_type, audit.entity_id) == (
        "relay.tracking_pasted",
        "relay_item",
        str(item),
    )
    assert CODE not in str(audit.after)  # the code is buyer data: never in the audit
    assert audit.after == {"carrier": "usps", "last4": "1234", "receipt_id": 51}


@pytest.mark.ac("AC-6.10.1")
async def test_ac_6_10_1_pasting_again_is_replay_safe() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    with pytest.raises(RelayItemMovedError):
        submit_tracking(
            auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE,
            now=PAID, correlation_id="req-2",
        )  # fmt: skip
    assert len(store.audits) == 1


@pytest.mark.ac("AC-6.10.2")
async def test_ac_6_10_2_a_number_on_the_etsy_receipt_closes_the_item() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    store.on_etsy.add((SHOP, 51, tracking_fingerprint(SHOP, CODE)))

    closed = submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    assert (closed.state, closed.closed_reason) == (
        RelayState.CLOSED,
        ClosedReason.TRACKING_ON_ETSY,
    )
    assert store.codes == {}  # never kept, never uploaded


async def test_a_number_already_used_for_another_order_is_refused() -> None:
    store = MemoryRelayStore()
    await opened(
        store, PAID + timedelta(days=3), orders=(AwaitingOrder(51, PAID), AwaitingOrder(53, PAID))
    )
    first, second = sorted(store.items.values(), key=lambda i: i.receipt_id)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=first.id, carrier="usps", code=CODE,
        now=PAID, correlation_id="req-1",
    )  # fmt: skip

    with pytest.raises(DuplicateTrackingError):
        submit_tracking(
            auth(), store, organization_id=ORG, item_id=second.id, carrier="usps", code=CODE,
            now=PAID, correlation_id="req-2",
        )  # fmt: skip
    assert store.items[second.id].state is RelayState.AWAITING_TRACKING


# --- Who may see and paste ---------------------------------------------------------------------


async def test_a_role_without_buyer_data_may_not_paste() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    with pytest.raises(PermissionError):
        submit_tracking(
            auth(Role.VIEWER), store, organization_id=ORG, item_id=item, carrier="usps",
            code=CODE, now=PAID, correlation_id="req-1",
        )  # fmt: skip
    assert store.codes == {}


async def test_another_organizations_or_an_ungranted_shops_item_is_not_visible() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    with pytest.raises(RelayItemNotVisibleError):
        submit_tracking(
            auth(Role.OPERATIONS, shops=frozenset({OTHER_SHOP})), store, organization_id=ORG,
            item_id=item, carrier="usps", code=CODE, now=PAID, correlation_id="req-1",
        )  # fmt: skip
    with pytest.raises(RelayItemNotVisibleError):
        submit_tracking(
            auth(), store, organization_id=ORG, item_id=uuid.uuid4(), carrier="usps", code=CODE,
            now=PAID, correlation_id="req-1",
        )  # fmt: skip


async def test_the_list_holds_the_open_items_of_the_callers_shops_only() -> None:
    store = MemoryRelayStore()
    await an_item(store)

    assert [
        i.receipt_id for i in list_relay_items(auth(), store, organization_id=ORG, now=PAID)
    ] == [51]
    assert (
        list_relay_items(
            auth(Role.OPERATIONS, shops=frozenset({OTHER_SHOP})),
            store,
            organization_id=ORG,
            now=PAID,
        )
        == ()
    )
    with pytest.raises(PermissionError):
        list_relay_items(auth(Role.VIEWER), store, organization_id=ORG, now=PAID)


# --- Close on evidence (AC-6.10.2) -------------------------------------------------------------


@pytest.mark.ac("AC-6.10.2")
async def test_ac_6_10_2_tracking_seen_on_etsy_closes_the_order_s_item_and_drops_its_code() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    assert store.close_order(SHOP, 51, reason=ClosedReason.TRACKING_ON_ETSY, now=PAID) is True
    assert store.items[item].closed_reason is ClosedReason.TRACKING_ON_ETSY
    assert store.codes == {}
    assert store.close_order(SHOP, 51, reason=ClosedReason.TRACKING_ON_ETSY, now=PAID) is False


@pytest.mark.ac("AC-6.10.2")
async def test_ac_6_10_2_tracking_recorded_from_etsy_closes_the_relay_item() -> None:
    """The recorder (Tracking Synchronization) closes the order's item on that evidence."""
    from htco_application.incidents import IncidentService  # noqa: PLC0415
    from htco_application.tracking_sync import (  # noqa: PLC0415
        ObservedVia,
        TrackingRecorder,
        TrackingSource,
    )
    from tests.fulfillment_fakes import MemoryIncidentStore, MemoryTrackingStore  # noqa: PLC0415

    store = MemoryRelayStore()
    item = await an_item(store)
    recorder = TrackingRecorder(
        MemoryTrackingStore(),
        IncidentService(MemoryIncidentStore(), clock=lambda: PAID),
        clock=lambda: PAID,
        relay=store,
    )

    await recorder.record(
        organization_id=ORG, shop_id=SHOP, receipt_id=51, carrier_name="USPS", code=CODE,
        source=TrackingSource.ETSY, observed_via=ObservedVia.SCHEDULE, notified_at=None,
        actor=RELAY_ACTOR,
    )  # fmt: skip

    assert store.items[item].closed_reason is ClosedReason.TRACKING_ON_ETSY


async def test_the_inspection_opens_the_due_relay_items() -> None:
    """Incident Detection's inspect mode opens them, beside the SLA incidents (slice 6.10)."""
    from htco_application.automation.durable import Completed, HandlerContext  # noqa: PLC0415
    from htco_application.execution.gateway import UnavailableGateway  # noqa: PLC0415
    from htco_application.fulfillment_schedule import INCIDENT_DETECTION  # noqa: PLC0415
    from htco_application.incidents import IncidentService  # noqa: PLC0415
    from htco_application.sla_inspector import IncidentDetection  # noqa: PLC0415
    from tests.fulfillment_fakes import (  # noqa: PLC0415
        MemoryIncidentStore,
        MemorySlaStore,
        MemoryTrackingStore,
    )

    now = PAID + timedelta(days=3)
    sla = MemorySlaStore(MemoryTrackingStore())
    sla.awaiting[SHOP] = [AwaitingOrder(51, PAID)]
    store = MemoryRelayStore()
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    detection = IncidentDetection(
        sla, IncidentService(MemoryIncidentStore(), clock=lambda: now), clock=lambda: now,
        alerts=NotificationService(notes, clock=lambda: now), relay=store,
    )  # fmt: skip
    ctx = HandlerContext(
        execution_id=uuid.uuid7(), automation_id=INCIDENT_DETECTION, actor=RELAY_ACTOR,
        organization_id=ORG, shop_id=SHOP, correlation_id="sched:1", causation_id=None,
        attempt=1, payload={"mode": "inspect"}, gateway=UnavailableGateway(),
    )  # fmt: skip

    outcome = await detection(ctx)

    assert isinstance(outcome, Completed)
    assert outcome.output["relay_items_opened"] == 1
    assert [i.receipt_id for i in store.items.values()] == [51]
