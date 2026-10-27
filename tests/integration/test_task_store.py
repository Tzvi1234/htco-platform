"""Tasks on real Postgres (slice 7.1), as the worker's role in its job's organization: one task
per idempotency key even when created concurrently, the machine and the evidence rule in the
database, one event per step, and the reader's organization and shop filters."""

import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from htco_application.tasks import StaleTask, StoredTask, TaskService, TaskStep
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.tasks import BusinessTask, EvidenceKind, TaskEvidence, TaskKind, TaskState
from htco_persistence.database import Database, tenant_scope
from htco_persistence.tasks import PostgresTaskReader, PostgresTaskStore
from tests.integration.test_etsy_connection import _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime(2026, 10, 26, 22, 0, tzinfo=UTC)
SYSTEM = ActorRef(ActorKind.SYSTEM, "test")
# A test correlation ID, never a secret: a constant, so gitleaks never reads it next to a key.
CORRELATION = "corr-db-tasks"


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
    db = Database(_url(), min_size=1, max_size=8, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def organization(admin: Database) -> tuple[uuid.UUID, uuid.UUID]:
    org, shop = uuid.uuid4(), uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.organizations (id, name) values (%s, %s)", [org, f"Tasks {org}"]
        )
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Shop', 'etsy', 'active')",
            [shop, org],
        )
    return org, shop


def a_task(org: uuid.UUID, key: str, *, shop: uuid.UUID | None = None) -> StoredTask:
    task = BusinessTask(
        id=uuid.uuid7(), organization_id=org, shop_id=shop, kind=TaskKind.HUMAN_CHANNEL,
        title="Log in to Etsy again", assignee=SYSTEM, state=TaskState.CREATED,
        idempotency_key=key, correlation_id=CORRELATION,
    )  # fmt: skip
    return StoredTask(task, created_at=NOW, updated_at=NOW)


def created_step(stored: StoredTask) -> TaskStep:
    t = stored.task
    return TaskStep(t.id, t.organization_id, None, TaskState.CREATED, SYSTEM, NOW)


@pytest.mark.ac("AC-7.1.2")
async def test_create__concurrently_with_one_key__one_task_one_step_one_event(
    admin: Database, worker_db: Database
) -> None:
    org, _ = organization(admin)
    store = PostgresTaskStore(worker_db)

    def create(_: int) -> tuple[StoredTask, bool]:
        stored = a_task(org, "hc:race")
        with tenant_scope(org):
            return store.create(stored, created_step(stored), ())

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(create, range(6)))

    assert sum(created for _, created in results) == 1
    assert len({stored.task.id for stored, _ in results}) == 1
    assert scalar(admin, "select count(*) from app.tasks where organization_id = %s", [org]) == 1
    assert (
        scalar(admin, "select count(*) from app.task_events where organization_id = %s", [org]) == 1
    )


@pytest.mark.ac("AC-7.1.3")
async def test_the_service__moves_to_completed_with_evidence_and_events(
    admin: Database, worker_db: Database
) -> None:
    org, shop = organization(admin)
    service = TaskService(PostgresTaskStore(worker_db), clock=lambda: NOW)
    with tenant_scope(org):
        stored, _ = await service.create(
            organization_id=org, shop_id=shop, kind=TaskKind.RELAY, title="Relay tracking",
            assignee=SYSTEM, idempotency_key="relay:1", correlation_id="c", actor=SYSTEM,
        )  # fmt: skip
        for state in (TaskState.QUEUED, TaskState.CLAIMED, TaskState.RUNNING):
            await service.move(org, stored.task.id, state, actor=SYSTEM)
        done = await service.move(
            org, stored.task.id, TaskState.COMPLETED, actor=SYSTEM,
            evidence=TaskEvidence(EvidenceKind.RESOURCE, "receipt:7"),
        )  # fmt: skip

    assert done.task.state is TaskState.COMPLETED
    assert done.task.evidence == TaskEvidence(EvidenceKind.RESOURCE, "receipt:7")
    events = scalar(
        admin,
        "select count(*) from jobs.outbox where organization_id = %s"
        " and payload ->> 'event_type' like 'tasks.%%'",
        [org],
    )
    assert events == 5  # created + four steps
    detail = PostgresTaskReader(admin).task_detail(org, stored.task.id)
    assert detail is not None
    assert [s.to_state for s in detail.history][-1] is TaskState.COMPLETED


async def test_move__a_stale_task__is_refused(admin: Database, worker_db: Database) -> None:
    org, _ = organization(admin)
    store = PostgresTaskStore(worker_db)
    stored = a_task(org, "stale")
    with tenant_scope(org):
        store.create(stored, created_step(stored), ())
        queued = StoredTask(stored.task.transition(TaskState.QUEUED), NOW, NOW)
        step = TaskStep(stored.task.id, org, TaskState.CREATED, TaskState.QUEUED, SYSTEM, NOW)
        store.move(stored, queued, step, ())
        with pytest.raises(StaleTask):
            store.move(stored, queued, step, ())


async def test_the_worker__never_reaches_another_organizations_task(
    admin: Database, worker_db: Database
) -> None:
    org, _ = organization(admin)
    other, _ = organization(admin)
    store = PostgresTaskStore(worker_db)
    stored = a_task(org, "mine")
    with tenant_scope(org):
        store.create(stored, created_step(stored), ())
    with tenant_scope(other):
        assert store.get(org, stored.task.id) is None


async def test_the_reader__filters_by_organization_shop_state_and_pages(
    admin: Database, worker_db: Database
) -> None:
    org, shop = organization(admin)
    other, _ = organization(admin)
    store = PostgresTaskStore(worker_db)
    for o, key, s in ((org, "a", None), (org, "b", shop), (org, "c", shop), (other, "d", None)):
        stored = a_task(o, key, shop=s)
        with tenant_scope(o):
            store.create(stored, created_step(stored), ())
    reader = PostgresTaskReader(admin)

    everything = reader.list_tasks(org, limit=10)
    shop_only = reader.list_tasks(org, limit=10, shop_ids=frozenset({shop}))
    first = reader.list_tasks(org, limit=2)
    rest = reader.list_tasks(org, limit=10, before=first[-1].task.id)
    queued = reader.list_tasks(org, limit=10, states=frozenset({TaskState.QUEUED}))

    assert len(everything) == 3
    assert {t.task.shop_id for t in shop_only} == {shop}
    assert len(first) + len(rest) == 3
    assert not {t.task.id for t in first} & {t.task.id for t in rest}
    assert queued == ()
