"""The Action Gateway against real Postgres (slice 5.12): `app.action_executions`, its audit
records and `actions.executed.v1` in the outbox."""

import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from htco_application.execution.gateway import ActionOutcome, ActionRef, ActionRequest, Channel
from htco_application.execution.service import (
    EVENT_DESTINATION,
    ActionGatewayService,
    Claim,
    GatewayParts,
    request_hash,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_persistence.actions import PostgresActionStore
from htco_persistence.database import Database, tenant_scope
from tests.application.test_action_gateway import office
from tests.execution_fakes import (
    Approvals,
    Capabilities,
    FakeChannel,
    Principals,
    Shops,
    real_catalog,
)
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)
SECRET = "12345678.FAKE-TOKEN-NEVER-STORED"  # noqa: S105 - a fake to search for


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=6)
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


SHOPS: dict[uuid.UUID, uuid.UUID] = {}


def shop_of(admin: Database, tenant: Tenant) -> uuid.UUID:
    """One shop per test organization, with its receipts capability."""
    if tenant.organization_id not in SHOPS:
        shop = uuid.uuid4()
        with admin.connection() as connection:
            connection.execute(
                "insert into app.shops (id, organization_id, name, marketplace, status) "
                "values (%s, %s, 'Action test shop', 'etsy', 'active')",
                [shop, tenant.organization_id],
            )
        SHOPS[tenant.organization_id] = shop
    return SHOPS[tenant.organization_id]


def ask(tenant: Tenant, key: str | None = None, **changes: Any) -> ActionRequest:
    values: dict[str, Any] = {
        "shop_id": SHOPS.get(tenant.organization_id),
        "action": ActionRef("etsy.receipts.read", 1),
        "actor": ActorRef(ActorKind.AUTOMATION, "etsy-order-intake"),
        "organization_id": tenant.organization_id,
        "idempotency_key": key or f"receipts:{uuid.uuid4()}",
        "correlation_id": "corr-db",
        "input": {"limit": 5},
    } | changes
    return ActionRequest(**values)


def gateway(database: Database, channel: FakeChannel) -> ActionGatewayService:
    registry = office()
    return ActionGatewayService(
        GatewayParts(
            catalog=real_catalog(registry),
            registry=lambda: registry,
            store=PostgresActionStore(database),
            principals=Principals(),
            shops=Shops(),
            approvals=Approvals(),
            adapters={Channel.API: channel},
            capabilities=Capabilities({(shop, "receipts_read") for shop in SHOPS.values()}),
            clock=lambda: NOW,
        )
    )


@pytest.mark.ac("AC-5.12.1")
async def test_claim__concurrently__one_row_and_one_winner(admin: Database) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    request = ask(tenant)
    store = PostgresActionStore(admin)
    claims: list[Claim] = []
    barrier = threading.Barrier(5)

    def claim() -> None:
        barrier.wait(timeout=10)
        claims.append(store.claim(request, request_hash=request_hash(request), now=NOW))

    threads = [threading.Thread(target=claim) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len({c.execution_id for c in claims}) == 1
    assert sum(c.created for c in claims) == 1


@pytest.mark.ac("AC-5.12.1")
@pytest.mark.ac("AC-5.12.4")
async def test_submit__runs_once_records_audit_and_event_and_keeps_no_r1_output(
    admin: Database,
) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    channel = FakeChannel(output={"receipts": [{"buyer": SECRET}]})
    service = gateway(admin, channel)
    request = ask(tenant)

    first = await service.submit(request)
    again = await service.submit(request)

    assert first.outcome is ActionOutcome.SUCCEEDED
    assert again.outcome is ActionOutcome.SUCCEEDED
    assert again.output is None  # R1 output (buyer data) is returned once, never stored
    assert len(channel.calls) == 1
    with admin.connection() as connection:
        row = connection.execute(
            "select state, output::text, reason from app.action_executions where id = %s",
            [first.execution_id],
        ).fetchone()
        events = connection.execute(
            "select destination, payload::text from jobs.outbox where action_execution_id = %s",
            [first.execution_id],
        ).fetchall()
        audits = connection.execute(
            "select after::text from audit.audit_log where entity_id = %s",
            [str(first.execution_id)],
        ).fetchall()
    assert row == ("succeeded", None, None)
    assert [e[0] for e in events] == [EVENT_DESTINATION]
    assert len(audits) == 1
    assert SECRET not in str(events) + str(audits)


async def test_finished_row__its_state_never_changes(admin: Database) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    result = await gateway(admin, FakeChannel(output={"receipts": []})).submit(ask(tenant))

    with admin.connection() as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update app.action_executions set state = 'running' where id = %s",
            [result.execution_id],
        )


async def test_denied__recorded_with_its_reason(admin: Database) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    request = ask(tenant, actor=ActorRef(ActorKind.ADVISOR, "jev"))

    result = await gateway(admin, FakeChannel()).submit(request)

    assert result.outcome is ActionOutcome.DENIED
    assert (
        scalar(
            admin, "select reason from app.action_executions where id = %s", [result.execution_id]
        )
        == "advisors never act"
    )


async def test_same_key_in_another_organization__a_separate_execution(admin: Database) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    shop_of(admin, tenant)
    shop_of(admin, other)
    channel = FakeChannel(output={"receipts": []})
    service = gateway(admin, channel)
    request = ask(tenant, key="shared-key")

    mine = await service.submit(request)
    theirs = await service.submit(
        replace(
            request, organization_id=other.organization_id, shop_id=SHOPS[other.organization_id]
        )
    )

    assert mine.execution_id != theirs.execution_id
    assert len(channel.calls) == 2


async def test_worker__sees_its_organizations_executions_only(
    admin: Database, worker_db: Database
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    shop_of(admin, tenant)
    shop_of(admin, other)
    result = await gateway(admin, FakeChannel(output={"receipts": []})).submit(ask(tenant))

    def visible() -> set[uuid.UUID]:
        with worker_db.connection() as connection:
            rows = connection.execute("select id from app.action_executions").fetchall()
        return {r[0] for r in rows}

    with tenant_scope(tenant.organization_id):
        assert result.execution_id in visible()
    with tenant_scope(other.organization_id):
        assert result.execution_id not in visible()
