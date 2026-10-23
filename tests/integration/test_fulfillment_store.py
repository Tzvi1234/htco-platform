"""Tracking events, SLA policies and incidents on real Postgres (slices 6.1 to 6.3), as the
worker's role in its job's organization: one row per parcel, one open incident per key even when
opened concurrently, the machine and the evidence rule in the database, events once."""

import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest

from htco_application.incidents import (
    INCIDENT_OPENED,
    INCIDENT_RESOLVED,
    Detection,
    IncidentService,
    order_resource,
)
from htco_application.order_ingest import IngestContext, IngestSource
from htco_application.sla_inspector import IncidentDetection
from htco_application.tracking_sync import ObservedVia, TrackingRecorder, TrackingSource
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
