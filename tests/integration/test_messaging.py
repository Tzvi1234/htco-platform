"""Inbox, outbox and stored `Idempotency-Key` responses against real Postgres (slice 5.2)."""

import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import anyio
import psycopg
import pytest

from htco_application.idempotency import (
    IdempotentRequest,
    KeyReused,
    Replay,
    Started,
    StillRunning,
    request_fingerprint,
)
from htco_application.jobs import JobSpec
from htco_application.messaging import (
    OUTBOX_DELIVERY_JOB,
    InboxEvent,
    OutboxMessage,
    RecordedEvent,
)
from htco_domain.jobs import FailurePlan, JobState
from htco_persistence.database import Database, tenant_scope
from htco_persistence.idempotency import PostgresIdempotencyStore
from htco_persistence.jobs import PostgresJobQueue
from htco_persistence.messaging import PostgresInbox, PostgresOutbox, add_outbox_in
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


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def event(event_id: str) -> InboxEvent:
    return InboxEvent(
        source="etsy",
        event_id=event_id,
        event_type="receipt.updated",
        payload={"receipt_id": 42},
        received_at=NOW,
    )


def ingest_job(event_id: str) -> JobSpec:
    return JobSpec(kind="test.ingest", idempotency_key=f"inbox:etsy:{event_id}")


@pytest.mark.ac("AC-5.2.1")
async def test_inbox__same_event_twice__one_row_and_one_job(admin: Database) -> None:
    inbox = PostgresInbox(admin)
    event_id = f"evt-{uuid.uuid4()}"

    first = inbox.record(event(event_id), ingest_job(event_id), now=NOW)
    second = inbox.record(event(event_id), ingest_job(event_id), now=NOW)

    assert (first.duplicate, second.duplicate) == (False, True)
    assert (second.inbox_id, second.job_id) == (first.inbox_id, first.job_id)
    assert scalar(admin, "select count(*) from jobs.inbox where event_id = %s", [event_id]) == 1
    assert (
        scalar(
            admin,
            "select count(*) from jobs.jobs where idempotency_key = %s",
            [f"inbox:etsy:{event_id}"],
        )
        == 1
    )


@pytest.mark.ac("AC-5.2.1")
async def test_inbox__same_event_at_the_same_time__one_row_and_one_job(admin: Database) -> None:
    inbox = PostgresInbox(admin)
    event_id = f"evt-{uuid.uuid4()}"
    results: list[RecordedEvent] = []
    barrier = threading.Barrier(4)

    def deliver() -> None:
        barrier.wait(timeout=10)
        results.append(inbox.record(event(event_id), ingest_job(event_id), now=NOW))

    threads = [threading.Thread(target=deliver) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4
    assert len({r.inbox_id for r in results}) == 1
    assert len({r.job_id for r in results}) == 1
    assert sum(not r.duplicate for r in results) == 1


async def test_inbox__keeps_the_payload_hash(admin: Database) -> None:
    event_id = f"evt-{uuid.uuid4()}"

    PostgresInbox(admin).record(event(event_id), ingest_job(event_id), now=NOW)

    stored = scalar(admin, "select payload_hash from jobs.inbox where event_id = %s", [event_id])
    assert len(bytes(stored)) == 32


def a_message(tenant: Tenant, key: str) -> OutboxMessage:
    return OutboxMessage(
        organization_id=tenant.organization_id,
        destination="test.sink",
        idempotency_key=key,
        payload={"receipt_id": "1"},
    )


async def test_outbox__same_key_twice__one_message_and_one_delivery_job(admin: Database) -> None:
    tenant = Tenant(admin)
    outbox = PostgresOutbox(admin)
    key = f"sink:{uuid.uuid4()}"

    first = outbox.add(a_message(tenant, key), now=NOW)
    second = outbox.add(a_message(tenant, key), now=NOW)

    assert first == second
    assert (
        scalar(
            admin,
            "select count(*) from jobs.jobs where kind = %s and idempotency_key = %s",
            [OUTBOX_DELIVERY_JOB, f"outbox:{first}"],
        )
        == 1
    )


async def test_outbox__the_worker_delivers_inside_the_organization_only(
    admin: Database, worker_db: Database
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    message_id = PostgresOutbox(admin).add(a_message(tenant, f"sink:{uuid.uuid4()}"), now=NOW)
    outbox = PostgresOutbox(worker_db)

    with tenant_scope(other.organization_id):
        assert await outbox.load(message_id) is None
    with tenant_scope(tenant.organization_id):
        pending = await outbox.load(message_id)
        assert pending is not None
        assert not pending.delivered
        await outbox.record_failure(message_id, error="ConnectionError", dead=False, now=NOW)
        await outbox.mark_delivered(message_id, now=NOW)
        delivered = await outbox.load(message_id)

    assert delivered is not None
    assert (delivered.delivered, delivered.attempts) == (True, 2)


async def test_outbox__a_failure_on_the_last_attempt__marks_it_dead(
    admin: Database, worker_db: Database
) -> None:
    tenant = Tenant(admin)
    message_id = PostgresOutbox(admin).add(a_message(tenant, f"sink:{uuid.uuid4()}"), now=NOW)

    with tenant_scope(tenant.organization_id):
        await PostgresOutbox(worker_db).record_failure(
            message_id, error="ConnectionError", dead=True, now=NOW
        )

    assert scalar(admin, "select status from jobs.outbox where id = %s", [message_id]) == "dead"


# --- Idempotency-Key ------------------------------------------------------------------------


def idempotent(user: uuid.UUID, key: str, body: bytes = b'{"a":1}') -> IdempotentRequest:
    return IdempotentRequest(
        user_id=user,
        key=key,
        method="POST",
        path="/v1/things",
        fingerprint=request_fingerprint("POST", "/v1/things", body),
    )


@pytest.mark.ac("AC-5.2.3")
async def test_idempotency_store__replays_refuses_and_expires(admin: Database) -> None:
    tenant = Tenant(admin)
    store = PostgresIdempotencyStore(admin)
    key = f"k-{uuid.uuid4().hex}"
    request = idempotent(tenant.owner, key)

    assert isinstance(await store.begin(request, now=NOW), Started)
    assert isinstance(await store.begin(request, now=NOW), StillRunning)
    await store.complete(request, status=202, body={"execution": "e-1"})
    assert await store.begin(request, now=NOW) == Replay(202, {"execution": "e-1"})
    assert isinstance(await store.begin(idempotent(tenant.owner, key, b"{}"), now=NOW), KeyReused)
    assert isinstance(await store.begin(idempotent(tenant.member, key), now=NOW), Started)
    later = NOW + timedelta(hours=24, seconds=1)
    assert isinstance(await store.begin(request, now=later), Started)


async def test_idempotency_store__abandoned_key__can_be_used_again(admin: Database) -> None:
    tenant = Tenant(admin)
    store = PostgresIdempotencyStore(admin)
    request = idempotent(tenant.owner, f"k-{uuid.uuid4().hex}")

    await store.begin(request, now=NOW)
    await store.abandon(request)

    assert isinstance(await store.begin(request, now=NOW), Started)


@pytest.mark.ac("AC-5.2.3")
async def test_idempotency_store__concurrent_first_requests__one_runs(admin: Database) -> None:
    tenant = Tenant(admin)
    store = PostgresIdempotencyStore(admin)
    request = idempotent(tenant.owner, f"k-{uuid.uuid4().hex}")
    outcomes: list[object] = []

    async def begin() -> None:
        outcomes.append(await store.begin(request, now=NOW))

    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(begin)

    assert sum(isinstance(o, Started) for o in outcomes) == 1
    assert (
        scalar(
            admin,
            "select count(*) from private.idempotency_keys where user_id = %s",
            [tenant.owner],
        )
        == 1
    )


def _delivery_job_of(admin: Database, message_id: uuid.UUID) -> uuid.UUID:
    job_id: uuid.UUID = scalar(
        admin, "select id from jobs.jobs where idempotency_key = %s", [f"outbox:{message_id}"]
    )
    return job_id


async def test_outbox__follows_its_jobs_retry_time(admin: Database) -> None:
    """Codex review of #55: the outbox showed the failure time as its next attempt."""
    tenant = Tenant(admin)
    message_id = PostgresOutbox(admin).add(a_message(tenant, f"sink:{uuid.uuid4()}"), now=NOW)
    job_id = _delivery_job_of(admin, message_id)
    with admin.connection() as connection:
        connection.execute(
            "update jobs.jobs set state = 'running', attempts = 1, leased_by = 'w', "
            "lease_expires_at = %s where id = %s",
            [NOW + timedelta(minutes=1), job_id],
        )
    retry_at = NOW + timedelta(seconds=17)

    PostgresJobQueue(admin).fail(
        job_id,
        worker_id="w",
        error="ConnectionError",
        plan=FailurePlan(JobState.RETRY_SCHEDULED, retry_at),
        now=NOW,
    )

    assert (
        scalar(admin, "select next_attempt_at from jobs.outbox where id = %s", [message_id])
        == retry_at
    )


async def test_outbox__a_crash_on_the_last_delivery_attempt__marks_the_message_dead(
    admin: Database,
) -> None:
    """Codex review of #55: a crash bypasses the delivery code's own failure handling."""
    tenant = Tenant(admin)
    message = a_message(tenant, f"sink:{uuid.uuid4()}")
    with admin.connection() as connection, connection.transaction():
        message_id = add_outbox_in(connection, message, now=NOW)
        connection.execute(
            "update jobs.jobs set max_attempts = 1, state = 'running', attempts = 1, "
            "leased_by = 'crashed', lease_expires_at = %s where idempotency_key = %s",
            [NOW, f"outbox:{message_id}"],
        )

    PostgresJobQueue(admin).release_expired(now=NOW + timedelta(seconds=1))

    assert scalar(admin, "select status from jobs.outbox where id = %s", [message_id]) == "dead"


def an_action_execution(admin: Database, tenant: Tenant) -> uuid.UUID:
    """A row of `app.action_executions` (slice 5.12), which the outbox now references."""
    action = uuid.uuid7()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.action_executions (id, organization_id, action_name, "
            "action_version, idempotency_key, request_hash, state, actor_kind, actor_id, "
            "correlation_id) values (%s, %s, 'etsy.inventory.update', 1, %s, %s, 'running', "
            "'system', 'test', 'req-db')",
            [action, tenant.organization_id, f"k:{action}", "a" * 64],
        )
    return action


async def test_outbox__keeps_the_action_and_correlation(admin: Database) -> None:
    tenant = Tenant(admin)
    action = an_action_execution(admin, tenant)
    message = OutboxMessage(
        organization_id=tenant.organization_id,
        destination="etsy.inventory.update",
        idempotency_key=f"etsy:{uuid.uuid4()}",
        action_execution_id=action,
        correlation_id="req-db",
    )
    outbox = PostgresOutbox(admin)

    loaded = await outbox.load(outbox.add(message, now=NOW))

    assert loaded is not None
    assert (loaded.message.action_execution_id, loaded.message.correlation_id) == (
        action,
        "req-db",
    )
    assert (
        scalar(
            admin,
            "select correlation_id from jobs.jobs where idempotency_key = %s",
            [f"outbox:{loaded.id}"],
        )
        == "req-db"
    )


async def test_outbox__an_unknown_action_execution__refused(admin: Database) -> None:
    tenant = Tenant(admin)
    message = OutboxMessage(
        organization_id=tenant.organization_id,
        destination="etsy.inventory.update",
        idempotency_key=f"etsy:{uuid.uuid4()}",
        action_execution_id=uuid.uuid7(),
        correlation_id="req-db",
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        PostgresOutbox(admin).add(message, now=NOW)
