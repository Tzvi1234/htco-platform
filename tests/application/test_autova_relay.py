"""The AutoVA tracking relay (slice 6.10), its Human-task path until Phase 8A.

A paid order without tracking past the relay window (2 business days, ASSUMPTIONS A-20) opens
exactly one relay item; the owner pastes the number from AutoVA, which is validated before
anything happens; the item is handed to a person to copy into Etsy (no Etsy write here, 6.8 adds
the guarded upload); tracking that reaches the Etsy receipt closes it, and a number is never
used twice.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import override

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.automation.durable import HandlerContext
from htco_application.execution.gateway import UnavailableGateway
from htco_application.fulfillment_schedule import INCIDENT_DETECTION
from htco_application.incidents import IncidentService
from htco_application.notifications import NotificationRequest, NotificationService
from htco_application.relay import (
    LIST_LIMIT,
    RELAY_CODE_TTL,
    DuplicateTrackingError,
    RelayItem,
    RelayItemMovedError,
    RelayItemNotVisibleError,
    expire_relay_codes,
    list_relay_items,
    open_due_relay_items,
    reconcile_relay_items,
    relay_code,
    submit_tracking,
)
from htco_application.sla_inspector import AwaitingOrder, IncidentDetection
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import TrackingFormatError, tracking_fingerprint
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.notifications import NotificationKind
from htco_domain.relay import ClosedReason, RelayState
from tests.fulfillment_fakes import MemoryIncidentStore, MemorySlaStore, MemoryTrackingStore
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


def NONE(_shop: uuid.UUID, _receipts: tuple[int, ...]) -> frozenset[int]:  # noqa: N802
    """No order of these has tracking."""
    return frozenset()


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


async def test_the_code_is_given_back_for_the_copy_to_buyer_data_roles_only() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    assert (
        relay_code(auth(), store, organization_id=ORG, item_id=item, now=PAID, correlation_id=None)
        == CODE
    )
    with pytest.raises(RelayItemNotVisibleError):  # a viewer of the shop: not even that it exists
        relay_code(
            auth(Role.VIEWER),
            store,
            organization_id=ORG,
            item_id=item,
            now=PAID,
            correlation_id=None,
        )
    store.close(item, reason=ClosedReason.TRACKING_ON_ETSY, now=PAID)
    with pytest.raises(RelayItemNotVisibleError):  # closed: the code is gone
        relay_code(auth(), store, organization_id=ORG, item_id=item, now=PAID, correlation_id=None)


async def test_one_workspace_needs_no_organization_named() -> None:
    store = MemoryRelayStore()
    await an_item(store)
    assert len(list_relay_items(auth(), store, organization_id=None, now=PAID)) == 1


# --- Codex review of #159 ----------------------------------------------------------------------


async def test_a_lost_notification_is_sent_by_the_next_inspection() -> None:
    """The item stays; a notification that failed is created on the next run (deduplicated)."""

    class Failing(MemoryNotificationStore):
        @override
        def add_notifications(self, *args: object, **kwargs: object) -> int:
            raise ConnectionError("notification store down")

    store = MemoryRelayStore()
    now = PAID + timedelta(days=3)
    failing = Failing([Member(OWNER, "owner", "owner@example.test")])
    with pytest.raises(ConnectionError):
        await open_due_relay_items(
            store, NotificationService(failing, clock=lambda: now), organization_id=ORG,
            shop_id=SHOP, awaiting=(AwaitingOrder(51, PAID),), now=now, correlation_id="s:1",
        )  # fmt: skip
    assert len(store.items) == 1

    count, notes = await opened(store, now + timedelta(minutes=15))
    again, _ = await opened(store, now + timedelta(minutes=30))

    assert (count, again) == (0, 0)  # no second item
    [row] = notes.rows.values()
    assert row.params["relay_item_id"] == str(next(iter(store.items)))


async def test_the_relay_window_counts_the_shops_own_business_days() -> None:
    """A Monday payment with Tuesday a holiday opens on Thursday, not Wednesday."""
    from htco_domain.fulfillment import BusinessCalendar  # noqa: PLC0415

    holiday = BusinessCalendar(holidays=frozenset({PAID.date() + timedelta(days=1)}))
    store = MemoryRelayStore()
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    wednesday = PAID + timedelta(days=2, minutes=1)

    def run(now: datetime) -> object:
        return open_due_relay_items(
            store, NotificationService(notes, clock=lambda: now), organization_id=ORG,
            shop_id=SHOP, awaiting=(AwaitingOrder(51, PAID),), now=now, correlation_id="s:1",
            calendar=holiday,
        )  # fmt: skip

    assert await run(wednesday) == 0  # type: ignore[misc]
    assert await run(wednesday + timedelta(days=1)) == 1  # type: ignore[misc]


async def test_a_cancelled_or_refunded_order_closes_its_item_and_drops_its_code() -> None:
    """The inspection reconciles open items with closed orders (ClosedReason.CANCELLED)."""
    from htco_application.automation.durable import HandlerContext  # noqa: PLC0415
    from htco_application.execution.gateway import UnavailableGateway  # noqa: PLC0415
    from htco_application.fulfillment_schedule import INCIDENT_DETECTION  # noqa: PLC0415
    from htco_application.incidents import IncidentService  # noqa: PLC0415
    from htco_application.sla_inspector import IncidentDetection  # noqa: PLC0415
    from tests.fulfillment_fakes import (  # noqa: PLC0415
        MemoryIncidentStore,
        MemorySlaStore,
        MemoryTrackingStore,
    )

    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip
    now = PAID + timedelta(days=4)
    sla = MemorySlaStore(MemoryTrackingStore())
    sla.closed[SHOP] = {51: now - timedelta(hours=1)}  # cancelled on Etsy
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    detection = IncidentDetection(
        sla, IncidentService(MemoryIncidentStore(), clock=lambda: now), clock=lambda: now,
        alerts=NotificationService(notes, clock=lambda: now), relay=store,
    )  # fmt: skip
    ctx = HandlerContext(
        execution_id=uuid.uuid7(), automation_id=INCIDENT_DETECTION, actor=RELAY_ACTOR,
        organization_id=ORG, shop_id=SHOP, correlation_id="sched:2", causation_id=None,
        attempt=1, payload={"mode": "inspect"}, gateway=UnavailableGateway(),
    )  # fmt: skip

    await detection(ctx)

    assert store.items[item].closed_reason is ClosedReason.CANCELLED
    assert store.codes == {}


# --- Sage's SECURITY review of #159 ------------------------------------------------------------


async def test_a_pasted_code_never_outlives_its_ttl_the_item_expires() -> None:
    """A backstop: an item handed off 30 days ago without tracking on Etsy closes as expired,
    and its code goes with it."""
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    early = await reconcile_relay_items(
        store, lambda _shop, _receipts: {}, organization_id=ORG, shop_id=SHOP, tracked_orders=NONE,
        now=PAID + RELAY_CODE_TTL - timedelta(minutes=1),
    )  # fmt: skip
    late = await reconcile_relay_items(
        store, lambda _shop, _receipts: {}, organization_id=ORG, shop_id=SHOP, tracked_orders=NONE,
        now=PAID + RELAY_CODE_TTL + timedelta(minutes=1),
    )  # fmt: skip

    assert (early, late) == ((0, 0, 0), (0, 1, 0))
    assert store.items[item].closed_reason is ClosedReason.EXPIRED
    assert store.codes == {}


async def test_reconcile_reaches_items_beyond_the_first_page() -> None:
    """Codex on #159: with a full page of older items still waiting, a later handed-off item
    still expires and a later cancelled one still closes, so no code outlives its purpose."""
    store = MemoryRelayStore()
    for receipt in range(1, LIST_LIMIT + 1):  # a full page of older, untouched items
        store.open_item(
            RelayItem(
                id=uuid.uuid7(), organization_id=ORG, shop_id=SHOP, receipt_id=receipt,
                state=RelayState.AWAITING_TRACKING, paid_at=PAID,
                opened_at=PAID + timedelta(minutes=receipt), updated_at=PAID,
            )
        )  # fmt: skip
    late = PAID + timedelta(days=1)
    handed, cancelled = (
        RelayItem(
            id=uuid.uuid7(),
            organization_id=ORG,
            shop_id=SHOP,
            receipt_id=receipt,
            state=RelayState.AWAITING_TRACKING,
            paid_at=PAID,
            opened_at=late,
            updated_at=late,
        )
        for receipt in (9001, 9002)
    )
    store.open_item(handed)
    store.open_item(cancelled)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=handed.id, carrier="usps", code=CODE,
        now=late, correlation_id="req-1",
    )  # fmt: skip
    now = late + RELAY_CODE_TTL + timedelta(minutes=1)

    result = await reconcile_relay_items(
        store, lambda _shop, receipts: {r: now for r in receipts if r == 9002},
        organization_id=ORG, shop_id=SHOP, now=now, tracked_orders=NONE,
    )  # fmt: skip

    assert result == (1, 1, 0)
    assert store.items[handed.id].closed_reason is ClosedReason.EXPIRED
    assert store.items[cancelled.id].closed_reason is ClosedReason.CANCELLED
    assert store.codes == {}


async def test_a_developer_updates_incidents_but_never_pastes_or_sees_a_code() -> None:
    """Quill on #159 (M1, M2): DEVELOPER has UPDATE_INCIDENTS but no buyer data, so it is the
    role that tells the two rights apart. The paste is refused, the code is not there for it,
    and nothing is audited. (Moved from the routes test with option (a).)"""
    store = MemoryRelayStore()
    await opened(
        store, PAID + timedelta(days=3), orders=(AwaitingOrder(51, PAID), AwaitingOrder(52, PAID))
    )
    waiting, pasted = (i.id for i in sorted(store.items.values(), key=lambda i: i.receipt_id))
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=pasted, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip
    audits = list(store.audits)
    developer = auth(Role.DEVELOPER)

    with pytest.raises(PermissionError):
        submit_tracking(
            developer, store, organization_id=ORG, item_id=waiting, carrier="usps",
            code="9400100000000000005678", now=PAID, correlation_id="req-2",
        )  # fmt: skip
    with pytest.raises(RelayItemNotVisibleError):
        relay_code(
            developer, store, organization_id=ORG, item_id=pasted, now=PAID,
            correlation_id="req-3",
        )  # fmt: skip

    assert store.items[waiting].state is RelayState.AWAITING_TRACKING
    assert waiting not in store.codes
    assert store.audits == audits


# --- Local Codex on #159 ------------------------------------------------------------------------


async def test_an_item_whose_order_already_has_tracking_closes_on_it() -> None:
    """An item opened from a snapshot taken before Tracking Synchronization recorded the order's
    tracking closes as tracking_on_etsy, with its code."""
    store = MemoryRelayStore()
    item = await an_item(store)
    receipt = store.items[item].receipt_id

    result = await reconcile_relay_items(
        store, lambda _shop, _receipts: {}, organization_id=ORG, shop_id=SHOP, now=PAID,
        tracked_orders=lambda _shop, receipts: frozenset(r for r in receipts if r == receipt),
    )  # fmt: skip

    assert result == (0, 0, 1)
    assert store.items[item].closed_reason is ClosedReason.TRACKING_ON_ETSY


class StaleSnapshot(MemorySlaStore):
    """Tracking Synchronization records the order's tracking right after the inspector read
    the orders awaiting it, before the relay item is inserted."""

    meanwhile: set[int] = set()  # noqa: RUF012 - one per test

    @override
    def awaiting_tracking(self, shop_id: uuid.UUID) -> tuple[AwaitingOrder, ...]:
        snapshot = super().awaiting_tracking(shop_id)
        self.meanwhile |= {order.receipt_id for order in snapshot}
        return snapshot

    @override
    def tracked_orders(self, shop_id: uuid.UUID, receipt_ids: tuple[int, ...]) -> frozenset[int]:
        return super().tracked_orders(shop_id, receipt_ids) | (self.meanwhile & set(receipt_ids))


def inspection(
    sla: MemorySlaStore, store: MemoryRelayStore, alerts: NotificationService, now: datetime
) -> tuple[IncidentDetection, HandlerContext]:
    detection = IncidentDetection(
        sla, IncidentService(MemoryIncidentStore(), clock=lambda: now), clock=lambda: now,
        alerts=alerts, relay=store,
    )  # fmt: skip
    ctx = HandlerContext(
        execution_id=uuid.uuid7(), automation_id=INCIDENT_DETECTION, actor=RELAY_ACTOR,
        organization_id=ORG, shop_id=SHOP, correlation_id="sched:9", causation_id=None,
        attempt=1, payload={"mode": "inspect"}, gateway=UnavailableGateway(),
    )  # fmt: skip
    return detection, ctx


async def test_an_item_opened_from_a_stale_snapshot_closes_in_the_same_run() -> None:
    now = PAID + timedelta(days=4)
    sla = StaleSnapshot(MemoryTrackingStore())
    sla.awaiting[SHOP] = [AwaitingOrder(receipt_id=71, paid_at=PAID)]
    store = MemoryRelayStore()
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    detection, ctx = inspection(sla, store, NotificationService(notes, clock=lambda: now), now)

    await detection(ctx)

    [item] = store.items.values()
    assert item.closed_reason is ClosedReason.TRACKING_ON_ETSY


class FailingAlerts(NotificationService):
    @override
    async def notify(self, request: NotificationRequest) -> int:
        raise RuntimeError("the notification store is down")


async def test_a_failing_notification_never_keeps_an_expired_code() -> None:
    """The retention runs first: an expired code goes even when a notification later fails."""
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip
    now = PAID + RELAY_CODE_TTL + timedelta(minutes=1)
    sla = MemorySlaStore(MemoryTrackingStore())
    sla.awaiting[SHOP] = [AwaitingOrder(receipt_id=72, paid_at=PAID)]  # its relay item notifies
    notes = MemoryNotificationStore([Member(OWNER, "owner", "owner@example.test")])
    detection, ctx = inspection(sla, store, FailingAlerts(notes, clock=lambda: now), now)

    with pytest.raises(RuntimeError, match="notification store"):
        await detection(ctx)

    assert store.items[item].closed_reason is ClosedReason.EXPIRED
    assert item not in store.codes


async def test_a_code_past_its_ttl_is_never_revealed_and_goes() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip
    audits = len(store.audits)

    with pytest.raises(RelayItemNotVisibleError):
        relay_code(
            auth(), store, organization_id=ORG, item_id=item,
            now=PAID + RELAY_CODE_TTL + timedelta(minutes=1), correlation_id="req-2",
        )  # fmt: skip

    assert store.items[item].closed_reason is ClosedReason.EXPIRED
    assert item not in store.codes
    assert len(store.audits) == audits  # nothing revealed, so no reveal is audited


async def test_the_retention_job_expires_a_code_past_its_ttl_only() -> None:
    """Local Codex on #159: the retention is its own job, whatever Incident Detection does."""
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    early = await expire_relay_codes(
        store, shop_id=SHOP, now=PAID + RELAY_CODE_TTL - timedelta(minutes=1)
    )
    late = await expire_relay_codes(store, shop_id=SHOP, now=PAID + RELAY_CODE_TTL)

    assert (early, late) == (0, 1)
    assert store.items[item].closed_reason is ClosedReason.EXPIRED
    assert item not in store.codes


async def test_a_number_already_on_etsy_closes_the_item_in_the_paste_itself() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    store.on_etsy.add((SHOP, 51, tracking_fingerprint(SHOP, CODE)))

    closed = submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    assert closed.closed_reason is ClosedReason.TRACKING_ON_ETSY
    assert item not in store.codes
    assert [a.action for a in store.audits] == ["relay.closed_on_etsy_tracking"]


async def test_every_reveal_of_a_code_is_audited_without_the_code() -> None:
    store = MemoryRelayStore()
    item = await an_item(store)
    submit_tracking(
        auth(), store, organization_id=ORG, item_id=item, carrier="usps", code=CODE, now=PAID,
        correlation_id="req-1",
    )  # fmt: skip

    relay_code(auth(), store, organization_id=ORG, item_id=item, now=PAID, correlation_id="req-2")

    reveal = store.audits[-1]
    assert (reveal.action, reveal.entity_id, reveal.correlation_id) == (
        "relay.code_revealed",
        str(item),
        "req-2",
    )
    assert CODE not in str(reveal.after)
