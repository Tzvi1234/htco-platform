"""The durable automation engine against real Postgres (slice 5.10): the in-memory engine's cases
(tests/application/test_durable_engine.py) with the Postgres store, plus the database's own
guards (transition trigger, append-only events, the worker's scope) and the registry sync.

Each test registers its own automation (`test-<random>`), so tests never share executions.
"""

import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import psycopg
import pytest

from htco_application.automation.durable import (
    EXECUTION_JOB,
    DurableAutomationEngine,
    Handler,
)
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.automation.records import (
    DefinitionRow,
    document_hash,
    plan_sync,
    registry_from_rows,
)
from htco_application.execution.gateway import UnavailableGateway
from htco_application.workforce.files import read_registry
from htco_application.workforce.registry import parse_automation
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import (
    AUTOMATION_SPEC_SECTIONS,
    AutomationExecution,
    ExecutionState,
    TriggerKind,
)
from htco_persistence.automation import PostgresAutomationCatalog, PostgresExecutionStore
from htco_persistence.database import Database, tenant_scope
from htco_persistence.status import PostgresChainReader
from tests.automation_fakes import EffectBook, engine_document
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)
REGISTRY = Path(__file__).resolve().parents[2] / "registry"
WORKER = ActorRef(ActorKind.SYSTEM, "agent-worker:test")
ADMIN = ActorRef(ActorKind.USER, "6a100000-0000-0000-0000-000000000001")


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=8)
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


def register(admin: Database, **changes: Any) -> str:
    automation_id = f"test-{uuid.uuid4().hex[:10]}"
    document = engine_document(automation_id, **changes)
    PostgresAutomationCatalog(admin).upsert(
        [
            (
                parse_automation(document),
                DefinitionRow(document, document_hash(document), "local", NOW),
            )
        ]
    )
    return automation_id


def engine_on(
    database: Database, automation_id: str, handler: Handler | None, output_check: Any = None
) -> DurableAutomationEngine:
    catalog = PostgresAutomationCatalog(database)
    return DurableAutomationEngine(
        lambda: registry_from_rows(catalog.current()),
        PostgresExecutionStore(database),
        {} if handler is None else {automation_id: handler},
        clock=lambda: NOW,
        jitter=lambda: 1.0,
        gateway=UnavailableGateway(),
        offload=anyio.to_thread.run_sync,
        output_check=output_check,
    )


def trigger(automation_id: str, tenant: Tenant, key: str = "k-1") -> TriggerRequest:
    return TriggerRequest(
        automation_id=automation_id,
        trigger=TriggerKind.EVENT,
        actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"),
        idempotency_key=key,
        payload={"receipt_id": "3412987711"},
        correlation_id="corr-db",
        organization_id=tenant.organization_id,
    )


def events(admin: Database, execution_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with admin.connection() as connection:
        return connection.execute(
            """
            select from_state, to_state, actor_kind, actor_id, correlation_id
            from app.automation_execution_events where execution_id = %s
            order by occurred_at, created_at, id
            """,
            [execution_id],
        ).fetchall()


async def in_scope(
    tenant: Tenant, engine: DurableAutomationEngine, execution_id: uuid.UUID, *, worker: ActorRef
) -> AutomationExecution:
    """Run the worker's job for one execution inside its organization, as the worker does."""
    with tenant_scope(tenant.organization_id):
        return await engine.execute(execution_id, worker=worker)


@pytest.mark.ac("AC-5.10.1")
async def test_trigger__concurrently_from_many_callers__one_row_one_job_one_run(
    admin: Database, worker_db: Database
) -> None:
    tenant = Tenant(admin)
    book = EffectBook()
    automation = register(admin)
    api = engine_on(admin, automation, book.handle)
    found: list[uuid.UUID] = []
    barrier = threading.Barrier(6)

    def fire() -> None:
        barrier.wait(timeout=10)
        found.append(api.trigger(trigger(automation, tenant)).id)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    [execution_id] = set(found)
    worker = engine_on(worker_db, automation, book.handle)
    done = await in_scope(tenant, worker, execution_id, worker=WORKER)
    again = await in_scope(tenant, worker, execution_id, worker=WORKER)

    assert (
        scalar(
            admin,
            "select count(*) from app.automation_executions where automation_id = %s",
            [automation],
        )
        == 1
    )
    assert (
        scalar(
            admin,
            "select count(*) from jobs.jobs where kind = %s and payload->>'execution_id' = %s",
            [EXECUTION_JOB, str(execution_id)],
        )
        == 1
    )
    assert (done.state, again.state) == (ExecutionState.SUCCEEDED, ExecutionState.SUCCEEDED)
    assert book.calls == 1


@pytest.mark.ac("AC-5.10.2")
async def test_trigger__not_ready__refused_before_any_row(admin: Database) -> None:
    tenant = Tenant(admin)
    drafts = {name: {"status": "draft", "text": name} for name in AUTOMATION_SPEC_SECTIONS}
    automation = register(admin, readiness="draft", specification=drafts)

    with pytest.raises(AutomationRefused, match="not active"):
        engine_on(admin, automation, EffectBook().handle).trigger(trigger(automation, tenant))

    assert (
        scalar(
            admin,
            "select count(*) from app.automation_executions where automation_id = %s",
            [automation],
        )
        == 0
    )


@pytest.mark.ac("AC-5.10.4")
@pytest.mark.ac("AC-5.10.6")
async def test_failing_handler__retry_dead_letter_admin_retry_all_recorded(
    admin: Database, worker_db: Database
) -> None:
    tenant = Tenant(admin)
    book = EffectBook(failures=2)
    automation = register(admin)
    execution = engine_on(admin, automation, book.handle).trigger(trigger(automation, tenant))
    worker = engine_on(worker_db, automation, book.handle)

    first = await in_scope(tenant, worker, execution.id, worker=WORKER)
    last = await in_scope(tenant, worker, execution.id, worker=WORKER)
    retried = engine_on(admin, automation, book.handle).retry_dead_lettered(
        tenant.organization_id, execution.id, actor=ADMIN, correlation_id="req-admin"
    )
    done = await in_scope(tenant, worker, execution.id, worker=WORKER)

    assert first.state is ExecutionState.RETRY_SCHEDULED
    assert (last.state, last.last_error) == (ExecutionState.DEAD_LETTERED, "ConnectionError")
    assert retried.id == execution.id
    assert (done.state, done.attempts) == (ExecutionState.SUCCEEDED, 3)
    retry_at = scalar(
        admin,
        "select run_at from jobs.jobs where idempotency_key = %s",
        [f"execution:{execution.id}:2"],
    )
    assert NOW + timedelta(seconds=5) <= retry_at <= NOW + timedelta(seconds=10)
    assert (
        scalar(
            admin,
            "select count(*) from audit.audit_log where entity_id = %s and action = %s",
            [str(execution.id), "automation.execution_retried"],
        )
        == 1
    )
    path = [(row[0], row[1]) for row in events(admin, execution.id)]
    assert path == [
        (None, "pending"),
        ("pending", "running"),
        ("running", "failed"),
        ("failed", "retry_scheduled"),
        ("retry_scheduled", "running"),
        ("running", "failed"),
        ("failed", "dead_lettered"),
        ("dead_lettered", "retry_scheduled"),
        ("retry_scheduled", "running"),
        ("running", "succeeded"),
    ]
    assert {row[4] for row in events(admin, execution.id)} == {"corr-db", "req-admin"}


@pytest.mark.ac("AC-5.10.5")
async def test_run_left_running_by_a_dead_worker__succeeds_once(
    admin: Database, worker_db: Database
) -> None:
    tenant = Tenant(admin)
    book = EffectBook()
    automation = register(admin)
    execution = engine_on(admin, automation, book.handle).trigger(trigger(automation, tenant))
    with admin.connection() as connection:
        connection.execute(
            "update app.automation_executions set state = 'running', attempts = 1 where id = %s",
            [execution.id],
        )
    worker = engine_on(worker_db, automation, book.handle)

    done = await in_scope(tenant, worker, execution.id, worker=WORKER)

    assert (done.state, done.attempts) == (ExecutionState.SUCCEEDED, 2)
    assert book.effects == {"3412987711": 1}


@pytest.mark.ac("AC-5.10.6")
async def test_database__refuses_a_transition_outside_the_machine_and_event_edits(
    admin: Database,
) -> None:
    tenant = Tenant(admin)
    automation = register(admin)
    execution = engine_on(admin, automation, EffectBook().handle).trigger(
        trigger(automation, tenant)
    )

    with admin.connection() as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update app.automation_executions set state = 'succeeded' where id = %s",
            [execution.id],
        )
    with admin.connection() as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute(
            "update app.automation_execution_events set reason = 'x' where execution_id = %s",
            [execution.id],
        )


async def test_worker__cannot_load_another_organizations_execution(
    admin: Database, worker_db: Database
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    automation = register(admin)
    execution = engine_on(admin, automation, EffectBook().handle).trigger(
        trigger(automation, tenant)
    )
    store = PostgresExecutionStore(worker_db)

    with tenant_scope(other.organization_id):
        assert store.load(execution.id) is None
    with tenant_scope(tenant.organization_id):
        assert store.load(execution.id) is not None


@pytest.mark.ac("AC-5.10.7")
async def test_registry_sync__applied_twice__the_second_plan_is_empty(admin: Database) -> None:
    documents = read_registry(REGISTRY).automations
    ids = {str(document["id"]) for document in documents}
    catalog = PostgresAutomationCatalog(admin)

    def registry_rows() -> list[DefinitionRow]:
        return [row for row in catalog.current() if row.key[0] in ids]

    first = plan_sync(documents, registry_rows(), commit="abc1234", now=NOW)
    catalog.upsert(first.rows)
    second = plan_sync(documents, registry_rows(), commit="abc1234", now=NOW)

    assert not first.blocked
    assert second.is_empty
    assert {row.key[0] for row in registry_rows()} == ids


async def test_waiting__on_postgres_the_job_ends_and_a_resume_runs_it_once(
    admin: Database, worker_db: Database
) -> None:
    """ADR-0019: RUNNING -> WAITING (no lease held), then RETRY_SCHEDULED with a new job."""
    tenant = Tenant(admin)
    book = EffectBook(wait_for_approval=True)
    automation = register(admin)
    api = engine_on(admin, automation, book.handle)
    execution = api.trigger(trigger(automation, tenant))
    worker = engine_on(worker_db, automation, book.handle)

    waiting = await in_scope(tenant, worker, execution.id, worker=WORKER)
    resumed = api.resume(
        tenant.organization_id, execution.id, actor=ADMIN, reason="approval_decided"
    )
    done = await in_scope(tenant, worker, execution.id, worker=WORKER)

    assert waiting.state is ExecutionState.WAITING
    assert resumed.state is ExecutionState.RETRY_SCHEDULED
    assert done.state is ExecutionState.SUCCEEDED
    assert book.effects == {"3412987711": 1}
    path = [(row[0], row[1]) for row in events(admin, execution.id)]
    assert ("running", "waiting") in path
    assert ("waiting", "retry_scheduled") in path


async def test_succeeded__the_checked_output__stored_and_read_back_for_the_chain(
    admin: Database, worker_db: Database
) -> None:
    """#127 on Postgres: the worker stores the accepted output with the run, and the chain
    reader returns it; nothing else is stored for a run without one."""
    tenant = Tenant(admin)
    book = EffectBook()
    automation = register(admin)
    execution = engine_on(admin, automation, book.handle).trigger(trigger(automation, tenant))
    worker = engine_on(
        worker_db, automation, book.handle, output_check=lambda _automation, output: dict(output)
    )

    done = await in_scope(tenant, worker, execution.id, worker=WORKER)

    assert done.output == {"order": "3412987711"}
    assert scalar(
        admin, "select output from app.automation_executions where id = %s", [execution.id]
    ) == {"order": "3412987711"}  # fmt: skip
    records = PostgresChainReader(admin).records(tenant.organization_id, execution.id)
    assert records is not None
    assert records.execution.output == {"order": "3412987711"}


@pytest.mark.ac("AC-126.1")
async def test_kill_switch__an_automation_set_to_draft_cancels_its_queued_execution(
    admin: Database, worker_db: Database
) -> None:
    """#126: draft stops what is already queued, not only new triggers (on Postgres)."""
    tenant = Tenant(admin)
    book = EffectBook()
    automation = register(admin)
    execution = engine_on(admin, automation, book.handle).trigger(trigger(automation, tenant))
    drafts = {name: {"status": "draft", "text": name} for name in AUTOMATION_SPEC_SECTIONS}
    document = engine_document(automation, version=2, readiness="draft", specification=drafts)
    PostgresAutomationCatalog(admin).upsert(
        [
            (
                parse_automation(document),
                DefinitionRow(document, document_hash(document), "local", NOW),
            )
        ]
    )
    worker = engine_on(worker_db, automation, book.handle)

    stopped = await in_scope(tenant, worker, execution.id, worker=WORKER)

    assert stopped.state is ExecutionState.CANCELLED
    assert book.calls == 0
    assert events(admin, execution.id)[-1][:2] == ("pending", "cancelled")
