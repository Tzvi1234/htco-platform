"""One correlation ID from trigger to execution to job to action to outbox (AC-5.0.2,
ADR-0019's event envelope rule), against real Postgres."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import anyio
import pytest

from htco_application.automation.durable import (
    EXECUTION_JOB,
    Completed,
    DurableAutomationEngine,
    HandlerContext,
    HandlerOutcome,
)
from htco_application.automation.engine import TriggerRequest
from htco_application.automation.records import registry_from_rows
from htco_application.execution.gateway import ActionRef, ActionRequest, Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_persistence.actions import PostgresActionStore
from htco_persistence.automation import PostgresAutomationCatalog, PostgresExecutionStore
from htco_persistence.database import Database, tenant_scope
from tests.execution_fakes import Approvals, Capabilities, FakeChannel, Principals, Shops
from tests.integration.test_action_store import shop_of
from tests.integration.test_durable_engine import register
from tests.integration.test_etsy_connection import Tenant, _url
from tests.integration.test_execution_chain_store import an_inbox_event

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


@pytest.mark.ac("AC-5.0.2")
async def test_one_correlation_id__trigger_execution_job_action_and_outbox(
    admin: Database,
) -> None:
    from tests.application.test_action_gateway import office  # noqa: PLC0415
    from tests.execution_fakes import real_catalog  # noqa: PLC0415

    tenant = Tenant(admin)
    shop = shop_of(admin, tenant)
    automation_id = register(admin, allowed_actions=["etsy.receipts.read"])
    catalog = PostgresAutomationCatalog(admin)

    def registry() -> Registry:
        return registry_from_rows(catalog.current())

    gateway = ActionGatewayService(
        GatewayParts(
            catalog=real_catalog(office()), registry=registry, store=PostgresActionStore(admin),
            principals=Principals(), shops=Shops(), approvals=Approvals(),
            adapters={Channel.API: FakeChannel(Channel.API, output={"receipts": [], "count": 0})},
            capabilities=Capabilities({(shop, "receipts_read")}), clock=lambda: NOW,
        )
    )  # fmt: skip

    async def handle(context: HandlerContext) -> HandlerOutcome:
        await context.gateway.submit(
            ActionRequest(
                action=ActionRef("etsy.receipts.read", 1),
                actor=ActorRef(ActorKind.AUTOMATION, automation_id),
                organization_id=context.organization_id,
                shop_id=context.shop_id,
                idempotency_key=f"receipts:{context.execution_id}",
                correlation_id=context.correlation_id,
                causation_id=str(context.execution_id),
                input={"limit": 5},
            )
        )
        return Completed()

    engine = DurableAutomationEngine(
        registry, PostgresExecutionStore(admin), {automation_id: handle}, clock=lambda: NOW,
        jitter=lambda: 1.0, gateway=gateway, offload=anyio.to_thread.run_sync,
    )  # fmt: skip
    inbox_id = an_inbox_event(admin, tenant)
    correlation = f"corr-trace-{uuid.uuid4().hex[:12]}"
    execution = engine.trigger(
        TriggerRequest(
            automation_id=automation_id, trigger=TriggerKind.EVENT,
            actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"), idempotency_key=f"k-{inbox_id}",
            payload={"receipt_id": "1"}, correlation_id=correlation,
            organization_id=tenant.organization_id, shop_id=shop, causation_id=str(inbox_id),
        )
    )  # fmt: skip
    with tenant_scope(tenant.organization_id):
        await engine.execute(execution.id, worker=ActorRef(ActorKind.SYSTEM, "agent-worker:t"))

    with admin.connection() as connection:
        trigger_row = connection.execute(
            "select correlation_id, causation_id from app.automation_executions where id = %s",
            [execution.id],
        ).fetchone()
        jobs = connection.execute(
            "select correlation_id, causation_id from jobs.jobs "
            "where kind = %s and payload->>'execution_id' = %s",
            [EXECUTION_JOB, str(execution.id)],
        ).fetchall()
        actions = connection.execute(
            "select id, correlation_id, causation_id from app.action_executions "
            "where organization_id = %s and idempotency_key = %s",
            [tenant.organization_id, f"receipts:{execution.id}"],
        ).fetchall()
        [(action_id, _, _)] = actions
        outbox = connection.execute(
            "select correlation_id from jobs.outbox where action_execution_id = %s",
            [action_id],
        ).fetchall()
        changes = connection.execute(
            "select distinct correlation_id from app.automation_execution_events "
            "where execution_id = %s",
            [execution.id],
        ).fetchall()

    # The trigger (an inbox event) is the execution's cause; the execution is the job's and the
    # action's cause; every row carries the trigger's correlation ID.
    assert trigger_row == (correlation, str(inbox_id))
    assert jobs == [(correlation, str(execution.id))]
    assert [(c, k) for _, c, k in actions] == [(correlation, str(execution.id))]
    assert outbox
    assert {row[0] for row in outbox} == {correlation}
    assert changes == [(correlation,)]
