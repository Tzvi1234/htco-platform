"""Tracking events, SLA policies and incidents on real Postgres (slices 6.1 to 6.3), as the
worker's role in its job's organization: one row per parcel, one open incident per key even when
opened concurrently, the machine and the evidence rule in the database, events once."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta

import psycopg
import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.execution.gateway import ActionOutcome, ActionRequest, ActionResult
from htco_application.incidents import (
    INCIDENT_OPENED,
    INCIDENT_RESOLVED,
    Detection,
    IncidentService,
    order_resource,
)
from htco_application.marketplace import OrderReceipt, OrderShipment
from htco_application.order_ingest import IngestContext, IngestSource
from htco_application.order_receipts import shipments_to_json
from htco_application.shop_integrations import ShopIntegration
from htco_application.sla_inspector import IncidentDetection
from htco_application.tracking_sync import (
    ObservedVia,
    TrackingRecorder,
    TrackingSource,
    TrackingSync,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.incidents import (
    Cause,
    Evidence,
    EvidenceKind,
    IncidentState,
    ResolutionRefused,
    ResourceKind,
)
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_orders import PostgresOrderStore
from htco_persistence.fulfillment import (
    PostgresIncidentReader,
    PostgresIncidentStore,
    PostgresSlaStore,
    PostgresTrackingStore,
)
from tests.integration.test_etsy_connection import NOW, _url, scalar
from tests.integration.test_order_history import connected, order

pytestmark = [pytest.mark.db, pytest.mark.anyio]

ACTOR = ActorRef(ActorKind.AUTOMATION, "incident-detection-escalation")
WEBHOOK = IngestContext(IngestSource.WEBHOOK, correlation_id="corr-db-fulfillment")


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def count(database: Database, sql: str, params: list[object]) -> int:
    return int(scalar(database, sql, params))


TRACKING_OF = "from app.tracking_events where shop_id = %s and receipt_id = %s"


def outbox(
    database: Database,
    event_type: str,
    *,
    shop: uuid.UUID | None = None,
    organization: uuid.UUID | None = None,
) -> int:
    return count(
        database,
        "select count(*) from jobs.outbox where payload->>'event_type' = %s"
        " and (%s::uuid is null or shop_id = %s) and (%s::uuid is null or organization_id = %s)",
        [event_type, shop, shop, organization, organization],
    )


def detection(organization_id: uuid.UUID, shop_id: uuid.UUID, receipt_id: int) -> Detection:
    return Detection(
        organization_id=organization_id,
        cause=Cause.TRACKING_MISSING,
        resource_kind=ResourceKind.ORDER,
        resource_id=order_resource(shop_id, receipt_id),
        detected_at=NOW,
        source="sla_inspector",
        shop_id=shop_id,
        receipt_id=receipt_id,
    )


@pytest.mark.ac("AC-6.1.1")
async def test_worker__each_parcel_once_and_its_event_once(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    with tenant_scope(org):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(81)], context=WEBHOOK, now=NOW
        )
        incidents = IncidentService(PostgresIncidentStore(worker_db), clock=lambda: NOW)
        recorder = TrackingRecorder(PostgresTrackingStore(worker_db), incidents, clock=lambda: NOW)
        results = [
            await recorder.record(
                organization_id=org, shop_id=shop, receipt_id=81, carrier_name="USPS",
                code=code, source=TrackingSource.ETSY, observed_via=ObservedVia.SCHEDULE,
                notified_at=NOW, actor=ACTOR,
            )
            for code in ["FAKE-DB-TRACK-1", "fake db track 1", "FAKE-DB-TRACK-2"]
        ]  # fmt: skip

    assert [r.created for r in results] == [True, False, True]
    assert results[1].id == results[0].id
    assert count(database, f"select count(*) {TRACKING_OF}", [shop, 81]) == 2
    assert outbox(database, "fulfillment.tracking_detected.v1", shop=shop) == 2
    last4 = scalar(
        database, f"select string_agg(last4, ',' order by last4) {TRACKING_OF}", [shop, 81]
    )
    assert last4 == "ACK1,ACK2"


@pytest.mark.ac("AC-6.3.1")
async def test_worker__concurrent_openings_leave_one_open_incident(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    store = PostgresIncidentStore(worker_db)

    def open_once() -> bool:
        import anyio  # noqa: PLC0415

        with tenant_scope(org):
            service = IncidentService(store, clock=lambda: NOW)
            _, created = anyio.run(lambda: service.open(detection(org, shop, 91), actor=ACTOR))
        return created

    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: open_once(), range(8)))

    assert created.count(True) == 1
    assert (
        count(database, "select count(*) from app.incidents where organization_id = %s", [org]) == 1
    )
    assert outbox(database, INCIDENT_OPENED, organization=org) == 1


@pytest.mark.ac("AC-6.3.2")
async def test_worker__tracking_resolves_the_incident_with_evidence_in_the_database(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    with tenant_scope(org):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(82)], context=WEBHOOK, now=NOW
        )
        incidents = IncidentService(PostgresIncidentStore(worker_db), clock=lambda: NOW)
        incident, _ = await incidents.open(detection(org, shop, 82), actor=ACTOR)
        with pytest.raises(ResolutionRefused):
            await incidents.resolve_on_evidence(org, incident.dedup_key, None, actor=ACTOR)
        recorder = TrackingRecorder(PostgresTrackingStore(worker_db), incidents, clock=lambda: NOW)
        await recorder.record(
            organization_id=org, shop_id=shop, receipt_id=82, carrier_name="usps",
            code="FAKE-DB-TRACK-3", source=TrackingSource.ETSY, observed_via=ObservedVia.WEBHOOK,
            notified_at=None, actor=ACTOR,
        )  # fmt: skip

    states = scalar(
        database,
        "select string_agg(to_state, '>' order by occurred_at, created_at, to_state = 'resolved')"
        " from app.incident_events where incident_id = %s",
        [incident.id],
    )
    assert states == "detected>verifying>resolved"
    state = scalar(database, "select state from app.incidents where id = %s", [incident.id])
    assert state == "resolved"
    evidence = scalar(
        database,
        "select evidence_kind from app.incident_events"
        " where incident_id = %s and to_state = 'resolved'",
        [incident.id],
    )
    assert evidence == "tracking_present"
    assert outbox(database, INCIDENT_RESOLVED, shop=shop) == 1


async def test_database__resolved_without_evidence_and_illegal_steps_are_refused(
    database: Database,
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    incidents = IncidentService(PostgresIncidentStore(database), clock=lambda: NOW)
    incident, _ = await incidents.open(detection(org, shop, 83), actor=ACTOR)

    with database.connection() as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update app.incidents set state = 'resolved', resolved_at = now() where id = %s",
            [incident.id],
        )
    with database.connection() as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update app.incidents set state = 'approval_required' where id = %s", [incident.id]
        )
    resolved = await incidents.resolve_on_evidence(
        org,
        incident.dedup_key,
        Evidence(EvidenceKind.ORDER_CLOSED, NOW, "etsy_receipt", "receipt:83"),
        actor=ACTOR,
    )
    assert resolved is not None
    assert resolved.state is IncidentState.RESOLVED


async def test_worker__the_inspector_opens_one_incident_for_a_late_order(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    late = order(84, days_ago=30)
    from htco_application.automation.durable import HandlerContext  # noqa: PLC0415
    from htco_application.execution.gateway import UnavailableGateway  # noqa: PLC0415

    context = HandlerContext(
        execution_id=uuid.uuid7(), automation_id="incident-detection-escalation",
        actor=ACTOR, organization_id=org, shop_id=shop, correlation_id="sched:db",
        causation_id=None, attempt=1, payload={"mode": "inspect"}, gateway=UnavailableGateway(),
    )  # fmt: skip
    with tenant_scope(org):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [late], context=WEBHOOK, now=NOW
        )
        incidents = IncidentService(PostgresIncidentStore(worker_db), clock=lambda: NOW)
        inspector = IncidentDetection(PostgresSlaStore(worker_db), incidents, clock=lambda: NOW)
        for _ in range(3):
            await inspector(context)
        # A recent order is inside the generous default window.
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(85, days_ago=1)], context=WEBHOOK, now=NOW + timedelta(seconds=1)
        )
        await inspector(context)

    assert count(database, "select count(*) from app.incidents where shop_id = %s", [shop]) == 1
    late_id = scalar(database, "select receipt_id from app.incidents where shop_id = %s", [shop])
    assert late_id == 84
    assert outbox(database, "fulfillment.sla_breached.v1", shop=shop) == 1


async def test_worker__a_checked_receipt_is_due_again_only_after_it_changes_on_etsy(
    database: Database, worker_db: Database
) -> None:
    """Codex review of #79: the schedule reads each shipped receipt once per change."""
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    sent = replace(order(86), status="completed", is_shipped=True, shipments=())
    store = PostgresTrackingStore(worker_db)
    with tenant_scope(org):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [sent], context=WEBHOOK, now=NOW
        )
        before = store.untracked_shipped(shop, limit=10)
        store.mark_checked(shop, 86, etsy_updated_at=sent.updated_at, now=NOW)
        after = store.untracked_shipped(shop, limit=10)
        changed = replace(sent, updated_at=sent.updated_at + timedelta(hours=1))
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [changed], context=WEBHOOK, now=NOW
        )
        again = store.untracked_shipped(shop, limit=10)

    assert (before, after, again) == ((86,), (), (86,))


@dataclass
class _ShipmentsReadMeanwhile:
    """`etsy.shipments.read` answering with the version `seen`; before it answers, Etsy Order
    Intake commits a newer version of the receipt (the barrier between read and checkpoint)."""

    seen: OrderReceipt
    meanwhile: Callable[[], Awaitable[None]]

    async def submit(self, request: ActionRequest) -> ActionResult:  # noqa: ARG002 - the port
        await self.meanwhile()
        return ActionResult(
            execution_id=uuid.uuid7(),
            outcome=ActionOutcome.SUCCEEDED,
            attempts=1,
            output={"shipments": [shipments_to_json(self.seen)], "count": 1},
        )


@dataclass
class _Connected:
    integration: ShopIntegration

    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        return self.integration if shop_id == self.integration.shop_id else None


async def test_worker__a_version_stored_during_the_read_stays_due(
    database: Database, worker_db: Database
) -> None:
    """Codex post-merge review (owner gate v4): the schedule reads V1 (no code yet) while the
    intake stores V2 with a new parcel. The checkpoint is V1, the version read, so V2 stays due;
    and an older run that finishes last never moves the checkpoint back."""
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    v1 = replace(order(87), status="completed", is_shipped=True, shipments=())
    v2 = replace(
        v1,
        updated_at=v1.updated_at + timedelta(hours=1),
        shipments=(OrderShipment("fake-post", "FAKE-DB-PARCEL-02", NOW),),
    )
    orders = PostgresOrderStore(worker_db)
    store = PostgresTrackingStore(worker_db)
    checkpoint = "select etsy_updated_at from app.tracking_checks where shop_id = %s"

    async def intake_stores_v2() -> None:
        await orders.ingest_receipts(integration, [v2], context=WEBHOOK, now=NOW)

    with tenant_scope(org):
        await orders.ingest_receipts(integration, [v1], context=WEBHOOK, now=NOW)
        incidents = IncidentService(PostgresIncidentStore(worker_db), clock=lambda: NOW)
        sync = TrackingSync(
            TrackingRecorder(store, incidents, clock=lambda: NOW),
            store,
            _Connected(integration),
            clock=lambda: NOW,
        )
        context = HandlerContext(
            execution_id=uuid.uuid7(), automation_id="tracking-sync",
            actor=ACTOR, organization_id=org, shop_id=shop,
            correlation_id="corr-db-barrier", causation_id=None, attempt=1,
            payload={"mode": "schedule"},
            gateway=_ShipmentsReadMeanwhile(v1, intake_stores_v2),
        )  # fmt: skip
        outcome = await sync(context)
        read_at = scalar(database, checkpoint, [shop])
        due = store.untracked_shipped(shop, limit=10)
        store.mark_checked(shop, 87, etsy_updated_at=v2.updated_at, now=NOW)
        store.mark_checked(shop, 87, etsy_updated_at=v1.updated_at, now=NOW)  # the older run
        after = store.untracked_shipped(shop, limit=10)

    assert outcome == Completed({"receipts": 1, "recorded": 0, "via": "schedule"})
    assert read_at == v1.updated_at
    assert due == (87,)  # V2 was never read: still due
    assert scalar(database, checkpoint, [shop]) == v2.updated_at
    assert after == ()


async def test_reader__one_orders_incidents_and_keyset_pages(database: Database) -> None:
    """Review of clients#48: filters by shop and receipt, and pages newest first by keyset."""
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    incidents = IncidentService(PostgresIncidentStore(database), clock=lambda: NOW)
    for receipt in (91, 92, 93):
        await incidents.open(detection(org, shop, receipt), actor=ACTOR)
    reader = PostgresIncidentReader(database)

    [one] = reader.list_incidents(org, open_only=False, limit=10, shop_id=shop, receipt_id=92)
    assert (one.shop_id, one.receipt_id) == (shop, 92)
    assert reader.list_incidents(org, open_only=False, limit=10, shop_id=uuid.uuid4()) == ()

    first = reader.list_incidents(org, open_only=False, limit=2, shop_id=shop)
    rest = reader.list_incidents(org, open_only=False, limit=2, shop_id=shop, before=first[-1].id)
    receipts = [i.receipt_id or 0 for i in (*first, *rest)]
    assert sorted(receipts) == [91, 92, 93]
    assert len(rest) == 1


@pytest.mark.ac("AC-6.9.2")
async def test_worker__a_reviews_transaction_finds_its_order_in_its_own_shop_only(
    database: Database, worker_db: Database
) -> None:
    """Slice 6.9: the low-review alert links the order through the stored order items."""
    tenant, integration = await connected(database)
    other_tenant, other = await connected(database)
    with tenant_scope(tenant.organization_id):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(91), order(92)], context=WEBHOOK, now=NOW
        )
    with tenant_scope(other_tenant.organization_id):
        await PostgresOrderStore(worker_db).ingest_receipts(
            other, [order(93)], context=WEBHOOK, now=NOW
        )
    store = PostgresSlaStore(worker_db)

    with tenant_scope(tenant.organization_id):
        found = store.receipts_for_transactions(integration.shop_id, (910, 920, 930, 999))

    assert found == {910: 91, 920: 92}  # 930 is the other shop's, 999 is not stored
    assert store.receipts_for_transactions(integration.shop_id, ()) == {}


async def test_worker__tracked_orders_names_the_receipts_with_tracking(
    database: Database, worker_db: Database
) -> None:
    """Slice 6.10 (local Codex on #159): the relay closes an item whose order got tracking."""
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    with tenant_scope(org):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(95), order(96)], context=WEBHOOK, now=NOW
        )
        incidents = IncidentService(PostgresIncidentStore(worker_db), clock=lambda: NOW)
        recorder = TrackingRecorder(PostgresTrackingStore(worker_db), incidents, clock=lambda: NOW)
        await recorder.record(
            organization_id=org, shop_id=shop, receipt_id=95, carrier_name="USPS",
            code="FAKE-DB-TRACK-95", source=TrackingSource.ETSY,
            observed_via=ObservedVia.SCHEDULE, notified_at=NOW, actor=ACTOR,
        )  # fmt: skip
        store = PostgresSlaStore(worker_db)
        found = store.tracked_orders(shop, (95, 96, 97))
        none = store.tracked_orders(shop, ())

    assert found == frozenset({95})
    assert none == frozenset()


async def test_worker__etsy_tracking_closes_the_relay_item_with_its_code_in_one_write(
    database: Database, worker_db: Database
) -> None:
    """Local Codex on #159: Etsy's tracking closes the order's relay item and deletes its code in
    the tracking event's own transaction; an AutoVA source (the paste itself) never does, and
    reconciliation counts Etsy events only."""
    from htco_application.audit import AuditRecord  # noqa: PLC0415
    from htco_application.relay import RelayItem  # noqa: PLC0415
    from htco_domain.fulfillment import tracking_fingerprint  # noqa: PLC0415
    from htco_domain.relay import ClosedReason, RelayState  # noqa: PLC0415
    from htco_persistence.relay import PostgresRelayStore  # noqa: PLC0415

    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    api, worker = PostgresRelayStore(database), PostgresRelayStore(worker_db)
    items = {}
    with tenant_scope(org):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(101), order(102)], context=WEBHOOK, now=NOW
        )
        for receipt in (101, 102):
            opened = RelayItem(
                id=uuid.uuid7(), organization_id=org, shop_id=shop, receipt_id=receipt,
                state=RelayState.AWAITING_TRACKING, paid_at=NOW - timedelta(days=3),
                opened_at=NOW, updated_at=NOW,
            )  # fmt: skip
            worker.open_item(opened)
            items[receipt] = opened.id
    for receipt, item_id in items.items():
        code = f"9400100000000000000{receipt}"
        api.hand_off(
            item_id, carrier="usps", fingerprint=tracking_fingerprint(shop, code), last4="0101",
            code=code, now=NOW,
            audit=AuditRecord.new(
                organization_id=org, actor=ACTOR, action="relay.tracking_pasted",
                entity_type="relay_item", entity_id=str(item_id), after={"receipt_id": receipt},
            ),
        )  # fmt: skip
    with tenant_scope(org):
        incidents = IncidentService(PostgresIncidentStore(worker_db), clock=lambda: NOW)
        recorder = TrackingRecorder(PostgresTrackingStore(worker_db), incidents, clock=lambda: NOW)
        for receipt, source in ((101, TrackingSource.ETSY), (102, TrackingSource.AUTOVA_MANUAL)):
            await recorder.record(
                organization_id=org, shop_id=shop, receipt_id=receipt, carrier_name="USPS",
                code=f"FAKE-DB-{receipt}", source=source, observed_via=ObservedVia.SCHEDULE,
                notified_at=NOW, actor=ACTOR,
            )  # fmt: skip
        tracked = PostgresSlaStore(worker_db).tracked_orders(shop, (101, 102))

    closed, waiting = api.item(org, items[101]), api.item(org, items[102])
    assert closed is not None
    assert waiting is not None
    assert (closed.state, closed.closed_reason) == (
        RelayState.CLOSED,
        ClosedReason.TRACKING_ON_ETSY,
    )
    assert waiting.state is RelayState.HANDED_OFF
    codes = "select count(*) from app.relay_item_codes where relay_item_id = %s"
    assert (count(database, codes, [items[101]]), count(database, codes, [items[102]])) == (0, 1)
    assert tracked == frozenset({101})
