"""Queue, execution and sync health against real Postgres (slice 5.9): the snapshot's reads and
the audited retry of a dead job, one organization at a time."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.audit import AuditRecord
from htco_domain.actors import ActorKind, ActorRef
from htco_persistence.database import Database
from htco_persistence.operations_health import PostgresOperationsHealth
from tests.integration.test_action_store import shop_of
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def dead_job(
    admin: Database, tenant: Tenant, shop: uuid.UUID, kind: str = "etsy.webhook_event"
) -> uuid.UUID:
    job = uuid.uuid4()
    with admin.connection() as c:
        c.execute(
            """
            insert into jobs.jobs (id, kind, idempotency_key, organization_id, shop_id, payload,
              state, attempts, max_attempts, initial_backoff_ms, max_backoff_ms, run_at,
              last_error, finished_at)
            values (%s, %s, %s, %s, %s, %s::jsonb, 'dead', 8, 8, 1000, 60000, %s,
              'MarketplaceUnavailableError', %s)
            """,
            [job, kind, f"k-{job}", tenant.organization_id, shop,
             '{"execution_id": "' + str(job) + '"}', NOW, NOW - timedelta(minutes=5)],
        )  # fmt: skip
    return job


def audit(tenant: Tenant, job: uuid.UUID) -> AuditRecord:
    return AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=ActorRef(ActorKind.USER, str(tenant.owner)),
        action="job.dead_letter_retried",
        entity_type="job",
        entity_id=str(job),
        correlation_id="req-db",
    )


@pytest.mark.ac("AC-5.9.2")
async def test_snapshot__the_organizations_dead_letters_workers_syncs_and_intake(
    admin: Database,
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    shop, theirs = shop_of(admin, tenant), shop_of(admin, other)
    mine = dead_job(admin, tenant, shop)
    dead_job(admin, other, theirs)
    worker = f"agent-worker:{uuid.uuid4().hex[:8]}"
    with admin.connection() as c:
        c.execute(
            "insert into jobs.worker_heartbeats (worker_id, release, started_at, last_beat_at) "
            "values (%s, 'abc', %s, %s)",
            [worker, NOW - timedelta(hours=1), NOW - timedelta(seconds=10)],
        )
        c.execute(
            "insert into app.sync_cursors (organization_id, shop_id, resource, kind, "
            "last_started_at, last_succeeded_at) values (%s, %s, 'orders', 'sweep', %s, %s)",
            [tenant.organization_id, shop, NOW - timedelta(hours=2), NOW - timedelta(hours=2)],
        )
        c.execute(
            "insert into jobs.inbox (id, source, event_id, event_type, organization_id, payload, "
            "payload_hash, received_at) values (%s, 'etsy', %s, 'order.paid', %s, '{}'::jsonb, "
            "%s, %s)",
            [uuid.uuid4(), f"wh-{uuid.uuid4()}", tenant.organization_id, b"\x01" * 32,
             NOW - timedelta(minutes=3)],
        )  # fmt: skip

    found = PostgresOperationsHealth(admin).snapshot(tenant.organization_id, now=NOW)

    assert [d.id for d in found.dead_letters if d.shop_id is not None] == [mine]
    [letter] = [d for d in found.dead_letters if d.id == mine]
    assert letter.last_error == "MarketplaceUnavailableError"
    assert letter.execution_id == mine  # the payload's execution ID, nothing else of it
    assert worker in [w.worker_id for w in found.workers]
    assert [(s.shop_id, s.kind) for s in found.shops] == [(shop, "sweep")]
    assert [(w.event_type, w.received_last_day, w.unprocessed) for w in found.webhooks] == [
        ("order.paid", 1, 1)
    ]
    assert any(j.kind == "etsy.webhook_event" and j.state == "dead" for j in found.jobs)


@pytest.mark.ac("AC-5.9.2")
async def test_retry__queues_the_dead_job_and_writes_the_audit_in_one_transaction(
    admin: Database,
) -> None:
    tenant = Tenant(admin)
    job = dead_job(admin, tenant, shop_of(admin, tenant))
    store = PostgresOperationsHealth(admin)

    assert store.retry(tenant.organization_id, job, now=NOW, audit=audit(tenant, job)) is True
    assert store.retry(tenant.organization_id, job, now=NOW, audit=audit(tenant, job)) is False

    assert scalar(admin, "select state from jobs.jobs where id = %s", [job]) == "queued"
    assert scalar(admin, "select attempts from jobs.jobs where id = %s", [job]) == 0
    assert (
        scalar(
            admin,
            "select count(*) from audit.audit_log where entity_id = %s and action = %s",
            [str(job), "job.dead_letter_retried"],
        )
        == 1
    )


async def test_find_and_retry__another_organizations_job__none_and_nothing_changed(
    admin: Database,
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    theirs = dead_job(admin, other, shop_of(admin, other))
    store = PostgresOperationsHealth(admin)

    assert store.find(tenant.organization_id, theirs) is None
    assert (
        store.retry(tenant.organization_id, theirs, now=NOW, audit=audit(tenant, theirs)) is False
    )
    assert scalar(admin, "select state from jobs.jobs where id = %s", [theirs]) == "dead"
