"""Status management against real Postgres (slice 5.18): one paid order's webhook, its intake
execution and its action, read back as the chain view; every state change has its event."""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from htco_application.automation.durable import (
    Completed,
    DurableAutomationEngine,
    HandlerContext,
    HandlerOutcome,
)
from htco_application.automation.engine import TriggerRequest
from htco_application.automation.records import registry_from_rows
from htco_application.execution.gateway import ActionRef, ActionRequest, Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.status import EXECUTION_STATE_CHANGED, ChainEntryKind, build_chain
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_persistence.actions import PostgresActionStore
from htco_persistence.automation import PostgresAutomationCatalog, PostgresExecutionStore
from htco_persistence.database import Database, tenant_scope
from htco_persistence.status import PostgresChainReader
from tests.execution_fakes import Approvals, Capabilities, FakeChannel, Principals, Shops
from tests.integration.test_action_store import shop_of
from tests.integration.test_durable_engine import register
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


def an_inbox_event(admin: Database, tenant: Tenant) -> uuid.UUID:
    inbox_id = uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            """
            insert into jobs.inbox
              (id, source, event_id, event_type, organization_id, payload, payload_hash,
               received_at)
            values (%s, 'etsy', %s, 'receipt.paid', %s, '{}'::jsonb, %s, %s)
            """,
            [inbox_id, f"wh-{inbox_id}", tenant.organization_id, os.urandom(32),
             NOW - timedelta(seconds=30)],
        )  # fmt: skip
    return inbox_id


@pytest.mark.ac("AC-5.18.1")
@pytest.mark.ac("AC-5.18.2")
async def test_chain__webhook_execution_action_and_events_in_causal_order(
    admin: Database,
) -> None:
    from tests.execution_fakes import real_catalog  # noqa: PLC0415

    tenant = Tenant(admin)
    shop = shop_of(admin, tenant)
    automation_id = register(admin, allowed_actions=["etsy.receipts.read"])
    catalog = PostgresAutomationCatalog(admin)

    def registry() -> Registry:
        return registry_from_rows(catalog.current())

    from tests.application.test_action_gateway import office  # noqa: PLC0415

    gateway = ActionGatewayService(
        GatewayParts(
            catalog=real_catalog(office()), registry=registry, store=PostgresActionStore(admin),
            principals=Principals(), shops=Shops(), approvals=Approvals(),
            adapters={Channel.INTERNAL: FakeChannel(Channel.INTERNAL),
                      Channel.API: FakeChannel(Channel.API, output={"receipts": [], "count": 0})},
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
    execution = engine.trigger(
        TriggerRequest(
            automation_id=automation_id, trigger=TriggerKind.EVENT,
            actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"), idempotency_key=f"k-{inbox_id}",
            payload={"receipt_id": "1"}, correlation_id=f"corr-{inbox_id}",
            organization_id=tenant.organization_id, shop_id=shop, causation_id=str(inbox_id),
        )
    )  # fmt: skip
    with tenant_scope(tenant.organization_id):
        await engine.execute(execution.id, worker=ActorRef(ActorKind.SYSTEM, "agent-worker:t"))

    records = PostgresChainReader(admin).records(tenant.organization_id, execution.id)
    assert records is not None
    chain = build_chain(records)

    entries = chain.entries
    assert (entries[0].kind, entries[0].name) == (ChainEntryKind.TRIGGER, "etsy:receipt.paid")
    assert [e.at for e in entries] == sorted(e.at for e in entries)
    [action] = [e for e in entries if e.kind is ChainEntryKind.ACTION]
    assert (action.name, action.detail["channel"], action.detail["outcome"]) == (
        "etsy.receipts.read",
        "api",
        "succeeded",
    )
    names = [e.name for e in entries if e.kind is ChainEntryKind.EVENT]
    assert "actions.executed.v1" in names
    changes = [e for e in entries if e.kind is ChainEntryKind.EXECUTION]
    # AC-5.18.2: one automations.execution_state_changed.v1 per recorded state change.
    assert names.count(EXECUTION_STATE_CHANGED) == len(changes) == 3
    assert (
        scalar(
            admin,
            "select count(*) from app.automation_execution_events where execution_id = %s",
            [execution.id],
        )
        == 3
    )
    assert PostgresChainReader(admin).records(uuid.uuid4(), execution.id) is None
