"""The Action Gateway against real Postgres (slice 5.12): `app.action_executions`, its audit
records and `actions.executed.v1` in the outbox."""

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from htco_application.access.policy import Principal
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRef,
    ActionRequest,
    ActionResult,
    Channel,
)
from htco_application.execution.service import (
    EVENT_DESTINATION,
    ActionGatewayService,
    Claim,
    Finish,
    GatewayParts,
    StaleExecutionError,
    request_hash,
)
from htco_domain.actions import ActionState
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
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


def gateway(
    database: Database,
    channel: FakeChannel,
    *,
    owner_of: Tenant | None = None,
    approvals: Approvals | None = None,
) -> ActionGatewayService:
    """The gateway on Postgres. `owner_of`: `OWNER_USER` is that organization's Owner, and the
    inventory write (built, its flag on) is available, as in the gateway's unit tests."""
    registry = office()
    principals, shops = Principals(), Shops()
    if owner_of is not None:
        principals.by_user[(OWNER_USER, owner_of.organization_id)] = Principal(
            organization_id=owner_of.organization_id,
            role=Role.OWNER,
            assurance=AssuranceLevel.AAL2,
            reauthenticated_at=NOW,
            granted_shops=frozenset({SHOPS[owner_of.organization_id]}),
        )
        shops.owners[SHOPS[owner_of.organization_id]] = owner_of.organization_id
    return ActionGatewayService(
        GatewayParts(
            catalog=real_catalog(registry, built=owner_of is not None),
            registry=lambda: registry,
            store=PostgresActionStore(database),
            principals=principals,
            shops=shops,
            approvals=approvals or Approvals(),
            adapters={Channel.API: channel},
            capabilities=Capabilities({(shop, "receipts_read") for shop in SHOPS.values()}),
            clock=lambda: NOW,
            write_flags=frozenset({"etsy.inventory.update"}),
        )
    )


OWNER_USER = uuid.UUID("7c100000-0000-0000-0000-000000000001")


def resume_concurrently(
    service: ActionGatewayService, request: ActionRequest, callers: int = 3
) -> list[ActionResult]:
    """`callers` threads, each with its own event loop, resubmit at once (a barrier)."""
    results: list[ActionResult] = []
    barrier = threading.Barrier(callers)

    def submit() -> None:
        barrier.wait(timeout=10)
        results.append(asyncio.run(service.submit(request)))

    threads = [threading.Thread(target=submit) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == callers
    return results


async def test_resume__concurrently_from_deferred__one_channel_call_one_transition(
    admin: Database,
) -> None:
    # Codex post-merge review (owner gate v4): two workers resubmitting a deferred read after its
    # retry_at both called Etsy, and the loser's record hit the final-state trigger.
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    request = ask(tenant)
    first = await gateway(admin, FakeChannel(fail=ConnectionError("down"))).submit(request)
    with admin.connection() as connection:  # as a busy guard leaves it (no retry_at)
        connection.execute(
            "update app.action_executions set state = 'deferred', outcome = 'deferred', "
            "error_code = 'slot_busy' where id = %s",
            [first.execution_id],
        )
    channel = FakeChannel(output={"receipts": []}, delay=0.3)
    service = gateway(admin, channel)

    results = resume_concurrently(service, request)
    later = await service.submit(request)

    assert len(channel.calls) == 1
    assert sum(r.outcome is ActionOutcome.SUCCEEDED for r in results) == 1
    assert all(
        r.outcome is ActionOutcome.SUCCEEDED or r.error_code == "in_progress" for r in results
    )
    assert later.outcome is ActionOutcome.SUCCEEDED
    with admin.connection() as connection:
        row = connection.execute(
            "select state, generation, resuming_until from app.action_executions where id = %s",
            [first.execution_id],
        ).fetchone()
        events = connection.execute(
            "select count(*) from jobs.outbox where action_execution_id = %s "
            "and payload->>'outcome' = 'succeeded'",
            [first.execution_id],
        ).fetchone()
    assert row == ("succeeded", 1, None)  # one resumption won, its record released the lease
    assert events == (1,)


async def test_resume__concurrently_from_waiting_approval__the_write_runs_once(
    admin: Database,
) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    approvals = Approvals()
    channel = FakeChannel(output={"listing_id": 1}, delay=0.3)
    service = gateway(admin, channel, owner_of=tenant, approvals=approvals)
    write = ask(
        tenant,
        action=ActionRef("etsy.inventory.update", 1),
        actor=ActorRef(ActorKind.USER, str(OWNER_USER)),
        input={"listing_id": 1, "products": []},
    )

    pending = await service.submit(write)
    approved = replace(write, approval_id=approvals.approve_last())
    results = resume_concurrently(service, approved)
    again = await service.submit(approved)

    assert pending.outcome is ActionOutcome.PENDING_APPROVAL
    assert len(channel.calls) == 1
    assert sum(r.outcome is ActionOutcome.SUCCEEDED for r in results) == 1
    assert again.outcome is ActionOutcome.SUCCEEDED
    assert (
        scalar(admin, "select state from app.action_executions where id = %s", [again.execution_id])
        == "succeeded"
    )


async def test_record__at_a_stale_generation__raises_and_writes_nothing(admin: Database) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    request = ask(tenant)
    store = PostgresActionStore(admin)
    claim = store.claim(request, request_hash=request_hash(request), now=NOW)
    won = store.resume(
        claim.execution_id,
        state=ActionState.RECEIVED,
        generation=0,
        lease_until=NOW + timedelta(minutes=1),
        now=NOW,
    )
    lost = store.resume(
        claim.execution_id,
        state=ActionState.RECEIVED,
        generation=0,
        lease_until=NOW + timedelta(minutes=1),
        now=NOW,
    )
    stale = Finish(
        state=ActionState.DEFERRED,
        path=(ActionState.RECEIVED, ActionState.DEFERRED),
        result=ActionResult(
            execution_id=claim.execution_id, outcome=ActionOutcome.DEFERRED, attempts=0
        ),
        generation=0,
    )

    with pytest.raises(StaleExecutionError):
        store.record(claim.execution_id, stale, now=NOW)

    assert (won, lost) == (1, None)
    assert (
        scalar(admin, "select state from app.action_executions where id = %s", [claim.execution_id])
        == "received"
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
