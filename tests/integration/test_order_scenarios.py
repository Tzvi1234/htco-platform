"""The order automations against real Postgres (slice 5.11): Scenario G (one paid receipt
replayed through webhooks and reconciliation, concurrently and out of order, yields one order,
one `orders.ingested.v1`, one action row per execution, and units counted once) and Scenario B
part 1 (a failed CRM write is retried idempotently; exhausted, it is dead-lettered and recovered
by an audited admin retry). Also the two new automations' stores (AC-5.11.4, AC-5.11.5).

TESTING_STRATEGY §10. Invented values only."""

import logging
import random
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest

from htco_application.automation.durable import (
    Completed,
    DurableAutomationEngine,
    Handler,
    HandlerContext,
)
from htco_application.automation.engine import TriggerRequest
from htco_application.automation.records import registry_from_rows
from htco_application.execution.gateway import Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.marketplace import OrderReceipt
from htco_application.order_automations import (
    SALES_COUNTERS,
    ProductSalesCounters,
    StoredSnapshot,
    verify,
)
from htco_application.order_ingest import INGESTED, IngestContext, IngestPlan, IngestSource
from htco_application.order_intake import OrderIntake
from htco_application.order_receipts import receipt_to_json
from htco_application.shop_integrations import ShopIntegration
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import ExecutionState, TriggerKind
from htco_domain.orders import OrderState
from htco_persistence.actions import PostgresActionStore
from htco_persistence.automation import PostgresAutomationCatalog, PostgresExecutionStore
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_orders import PostgresOrderStore
from htco_persistence.order_automations import PostgresOrderAutomationStore
from tests.execution_fakes import Approvals, Capabilities, FakeChannel, Principals, Shops
from tests.integration.test_durable_engine import register
from tests.integration.test_etsy_connection import _url, scalar
from tests.integration.test_order_history import connected, order

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)
WORKER = ActorRef(ActorKind.SYSTEM, "agent-worker:scenario")
ADMIN = ActorRef(ActorKind.USER, "6a100000-0000-0000-0000-000000000009")


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
    db = Database(_url(), min_size=1, max_size=8, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@dataclass
class OneShop:
    integration: ShopIntegration

    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        return self.integration if shop_id == self.integration.shop_id else None


@dataclass
class FlakyStore:
    """The order store, failing its first `failures` writes (Scenario B's CRM write)."""

    store: PostgresOrderStore
    failures: int = 0
    calls: int = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def ingest_receipts(
        self, integration: ShopIntegration, receipts: Sequence[OrderReceipt], **kwargs: Any
    ) -> tuple[IngestPlan, ...]:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("the CRM write failed")
        return await self.store.ingest_receipts(integration, receipts, **kwargs)


@dataclass
class World:
    admin: Database
    worker_db: Database
    tenant: Any
    integration: ShopIntegration
    receipt: OrderReceipt
    automation: str
    store: FlakyStore = field(init=False)
    channel: FakeChannel = field(init=False)

    def __post_init__(self) -> None:
        self.store = FlakyStore(PostgresOrderStore(self.worker_db))
        self.channel = FakeChannel(
            Channel.API, output={"receipts": [receipt_to_json(self.receipt)], "count": 1}
        )

    def engine(self, database: Database) -> DurableAutomationEngine:
        from tests.application.test_action_gateway import office  # noqa: PLC0415
        from tests.execution_fakes import real_catalog  # noqa: PLC0415

        catalog = PostgresAutomationCatalog(database)

        def registry() -> Registry:
            return registry_from_rows(catalog.current())

        gateway = ActionGatewayService(
            GatewayParts(
                catalog=real_catalog(office()), registry=registry,
                store=PostgresActionStore(database), principals=Principals(), shops=Shops(),
                approvals=Approvals(), adapters={Channel.API: self.channel},
                capabilities=Capabilities({(self.integration.shop_id, "receipts_read")}),
                clock=lambda: NOW,
            )
        )  # fmt: skip
        handler: Handler = OrderIntake(store=self.store, shops=OneShop(self.integration),
                                       clock=lambda: NOW)  # fmt: skip
        return DurableAutomationEngine(
            registry, PostgresExecutionStore(database), {self.automation: handler},
            clock=lambda: NOW, jitter=lambda: 1.0, gateway=gateway,
            offload=anyio.to_thread.run_sync,
        )  # fmt: skip

    def trigger(self, key: str, payload: dict[str, Any]) -> uuid.UUID:
        request = TriggerRequest(
            automation_id=self.automation, trigger=TriggerKind.EVENT,
            actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"), idempotency_key=key,
            payload=payload, correlation_id=f"corr-{key}"[:200],
            organization_id=self.tenant.organization_id, shop_id=self.integration.shop_id,
        )  # fmt: skip
        return self.engine(self.admin).trigger(request).id

    async def run(self, execution_id: uuid.UUID) -> Any:
        with tenant_scope(self.tenant.organization_id):
            return await self.engine(self.worker_db).execute(execution_id, worker=WORKER)


async def world(admin: Database, worker_db: Database, receipt_id: int, **changes: Any) -> World:
    tenant, integration = await connected(admin)
    automation = register(admin, allowed_actions=["etsy.receipts.read"], **changes)
    return World(admin, worker_db, tenant, integration, order(receipt_id), automation)


def count(admin: Database, sql: str, params: list[object]) -> int:
    return int(scalar(admin, sql, params))


# --- Scenario G -----------------------------------------------------------------------------


@pytest.mark.ac("AC-5.11.2")
@pytest.mark.parametrize("replays", [1, 4, 9])
@pytest.mark.parametrize("seed", [5, 11])
async def test_scenario_g__one_receipt_replayed_concurrently_and_out_of_order__one_order(
    admin: Database, worker_db: Database, replays: int, seed: int
) -> None:
    w = await world(admin, worker_db, 51_000 + replays * 10 + seed)
    receipt_id = w.receipt.receipt_id
    shop = w.integration.shop_id
    # The deliveries of one paid receipt: Etsy's webhook (sent again), a later notice for the
    # same order, and the reconciliation run, in a random order.
    keys = [f"etsy:{shop}:{receipt_id}:order.paid" for _ in range(replays)]
    keys += [f"etsy:{shop}:{receipt_id}:order.updated", f"etsy:{shop}:{receipt_id}:order.paid"]
    random.Random(seed).shuffle(keys)  # noqa: S311 - a test order, not a secret
    executions = [
        w.trigger(key, {"receipt_id": receipt_id, "event_type": key.rsplit(":", 1)[-1]})
        for key in keys
    ]
    async with anyio.create_task_group() as group:
        for execution_id in set(executions):
            group.start_soon(w.run, execution_id)

    assert len(set(executions)) == 2  # one execution per distinct key
    key = [shop, receipt_id]
    assert (
        count(
            admin,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            key,
        )
        == 1
    )
    assert (
        count(
            admin,
            "select sum(quantity) from app.etsy_order_items where shop_id = %s and receipt_id = %s",
            key,
        )
        == 2
    )
    assert (
        count(
            admin,
            "select count(*) from jobs.outbox where idempotency_key = %s",
            [f"{INGESTED}:{shop}:{receipt_id}"],
        )
        == 1
    )
    # One action row per business effect: each execution read the receipt once.
    assert count(
        admin,
        "select count(*) from app.action_executions where organization_id = %s and action_name = 'etsy.receipts.read' and correlation_id like %s",  # noqa: E501
        [w.tenant.organization_id, f"corr-etsy:{shop}:{receipt_id}:%"],
    ) == 2  # fmt: skip
    # No duplicate outbox message for the same effect.
    assert count(
        admin,
        "select count(*) - count(distinct idempotency_key) from jobs.outbox where shop_id = %s",
        [shop],
    ) == 0  # fmt: skip

    # Units sold and order count, however often the counters run.
    counters = PostgresOrderAutomationStore(worker_db)
    with tenant_scope(w.tenant.organization_id):
        for _ in range(replays):
            await counters.recount(
                w.tenant.organization_id, shop, receipt_id, execution_id=uuid.uuid4(), now=NOW
            )
    sold = admin.connection()
    with sold as c:
        units, orders = c.execute(
            "select units, orders from app.product_sales_counters where shop_id = %s and listing_id = 777",  # noqa: E501
            [shop],
        ).fetchone() or (0, 0)
    assert (units, orders) == (2, 1)


# --- Scenario B part 1 ----------------------------------------------------------------------


@pytest.mark.ac("AC-5.11.3")
async def test_scenario_b__a_failed_crm_write__retried_idempotently_into_one_order(
    admin: Database, worker_db: Database
) -> None:
    w = await world(admin, worker_db, 52_001)
    w.store.failures = 1
    shop, receipt_id = w.integration.shop_id, w.receipt.receipt_id
    inbox = uuid.uuid4()
    with admin.connection() as c:
        c.execute(
            "insert into jobs.inbox (id, source, event_id, event_type, organization_id, "
            "shop_id, payload, payload_hash, received_at) values (%s, 'etsy', %s, 'order.paid', "
            "%s, %s, '{}'::jsonb, %s, %s)",
            [inbox, f"wh-{inbox}", w.tenant.organization_id, shop, b"\x02" * 32, NOW],
        )
    paid = {"receipt_id": receipt_id, "event_type": "order.paid"}
    execution = w.trigger(f"etsy:{shop}:{receipt_id}:order.paid", paid)

    first = await w.run(execution)

    assert first.state is ExecutionState.RETRY_SCHEDULED
    assert count(admin, "select count(*) from jobs.inbox where id = %s", [inbox]) == 1
    assert (
        count(
            admin,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            [shop, receipt_id],
        )
        == 0
    )

    second = await w.run(execution)
    again = await w.run(execution)

    assert (second.state, again.state) == (ExecutionState.SUCCEEDED, ExecutionState.SUCCEEDED)
    assert (
        count(
            admin,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            [shop, receipt_id],
        )
        == 1
    )


@pytest.mark.ac("AC-5.11.3")
async def test_scenario_b__retries_exhausted__dead_lettered_then_recovered_by_an_audited_retry(
    admin: Database, worker_db: Database
) -> None:
    w = await world(admin, worker_db, 52_002)  # two attempts (the test definition's policy)
    w.store.failures = 2
    shop, receipt_id = w.integration.shop_id, w.receipt.receipt_id
    paid = {"receipt_id": receipt_id, "event_type": "order.paid"}
    execution = w.trigger(f"etsy:{shop}:{receipt_id}:order.paid", paid)

    await w.run(execution)
    dead = await w.run(execution)
    retried = w.engine(admin).retry_dead_lettered(
        w.tenant.organization_id, execution, actor=ADMIN, correlation_id="req-scenario-b"
    )
    done = await w.run(execution)

    assert (dead.state, dead.last_error) == (ExecutionState.DEAD_LETTERED, "ConnectionError")
    assert retried.id == execution
    assert done.state is ExecutionState.SUCCEEDED
    assert (
        count(
            admin,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            [shop, receipt_id],
        )
        == 1
    )
    assert count(
        admin,
        "select count(*) from audit.audit_log where entity_id = %s and action = %s",
        [str(execution), "automation.execution_retried"],
    ) == 1  # fmt: skip


# --- the new automations' stores ------------------------------------------------------------


async def stored(
    admin: Database, worker_db: Database, receipt: OrderReceipt
) -> tuple[Any, ShopIntegration]:
    tenant, integration = await connected(admin)
    with tenant_scope(tenant.organization_id):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [receipt], context=IngestContext(IngestSource.WEBHOOK, correlation_id="c"),
            now=NOW,
        )  # fmt: skip
    return tenant, integration


@pytest.mark.ac("AC-5.11.4")
async def test_verification__a_matching_order_none__a_changed_one_drifts_once(
    admin: Database, worker_db: Database
) -> None:
    receipt = order(53_001)
    tenant, integration = await stored(admin, worker_db, receipt)
    store = PostgresOrderAutomationStore(worker_db)
    with tenant_scope(tenant.organization_id):
        snapshot = await store.snapshot(integration.shop_id, receipt)
        assert snapshot is not None
        assert verify(snapshot, receipt) == ()
        changed = replace(receipt, grand_total=replace(receipt.grand_total, amount=2700))
        changed_snapshot = await store.snapshot(integration.shop_id, changed)
        assert changed_snapshot is not None
        differences = verify(changed_snapshot, changed)
        first = await store.record_differences(integration, changed, differences,
                                               execution_id=uuid.uuid4(), correlation_id="c",
                                               now=NOW)  # fmt: skip
        second = await store.record_differences(integration, changed, differences,
                                                execution_id=uuid.uuid4(), correlation_id="c",
                                                now=NOW)  # fmt: skip

    assert [d.kind for d in differences] == ["totals"]
    assert (first, second) == (1, 0)
    assert (
        count(
            admin,
            "select count(*) from app.sync_drifts where shop_id = %s and receipt_id = %s "
            "and kind = 'totals' and source = 'verification'",
            [integration.shop_id, receipt.receipt_id],
        )
        == 1
    )
    assert (
        count(
            admin,
            "select count(*) from jobs.outbox where shop_id = %s "
            "and payload->>'event_type' = 'orders.drift_detected.v1'",
            [integration.shop_id],
        )
        == 1
    )


def test_snapshot_type__holds_no_buyer_field() -> None:
    assert set(StoredSnapshot.__dataclass_fields__) == {
        "etsy_updated_at", "total_matches", "items",
    }  # fmt: skip


@pytest.mark.ac("AC-5.11.5")
async def test_counters__a_cancellation_replayed__takes_the_units_back_once(
    admin: Database, worker_db: Database
) -> None:
    receipt = order(53_002)
    tenant, integration = await stored(admin, worker_db, receipt)
    store = PostgresOrderAutomationStore(worker_db)
    shop = integration.shop_id
    with tenant_scope(tenant.organization_id):
        await store.recount(tenant.organization_id, shop, receipt.receipt_id,
                            execution_id=uuid.uuid4(), now=NOW)  # fmt: skip
        cancelled = replace(
            receipt, status="canceled", updated_at=receipt.updated_at + timedelta(hours=1)
        )
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [cancelled],
            context=IngestContext(IngestSource.WEBHOOK, correlation_id="c"),
            now=NOW,
        )  # fmt: skip
        for _ in range(3):
            await store.recount(tenant.organization_id, shop, receipt.receipt_id,
                                execution_id=uuid.uuid4(), now=NOW)  # fmt: skip

    assert (
        scalar(
            admin,
            "select order_state from app.etsy_orders where shop_id = %s and receipt_id = %s",
            [shop, receipt.receipt_id],
        )
        == OrderState.CANCELLED.value
    )
    with admin.connection() as c:
        row = c.execute(
            "select units, orders from app.product_sales_counters where shop_id = %s and listing_id = 777",  # noqa: E501
            [shop],
        ).fetchone()
    assert row == (0, 0)


@pytest.mark.ac("AC-5.11.5")
async def test_counters__orders_stored_by_the_history_backfill__counted_by_the_rebuild_once(
    admin: Database, worker_db: Database
) -> None:
    """The backfill publishes no events (Codex P1 on #90): the rebuild counts its orders, and
    running it again, or recounting one of them from an event, moves nothing."""
    tenant, integration = await connected(admin)
    shop = integration.shop_id
    backfilled = [order(53_101), order(53_102), order(53_103, status="canceled")]
    await PostgresOrderStore(admin).save_backfill_page(
        integration, backfilled, next_offset=3, now=NOW
    )
    store = PostgresOrderAutomationStore(worker_db)
    handler = ProductSalesCounters(store, clock=lambda: NOW, page_size=2)

    def ctx() -> HandlerContext:
        return HandlerContext(
            execution_id=uuid.uuid7(), automation_id=SALES_COUNTERS, actor=ADMIN,
            organization_id=tenant.organization_id, shop_id=shop, correlation_id="c",
            causation_id=None, attempt=1, payload={"rebuild": True},
            gateway=None,  # type: ignore[arg-type]
        )  # fmt: skip

    with tenant_scope(tenant.organization_id):
        first = await handler(ctx())
        await store.recount(tenant.organization_id, shop, 53_101, execution_id=uuid.uuid4(),
                            now=NOW)  # fmt: skip
        again = await handler(ctx())

    assert first == Completed({"rebuilt": 3, "moved": 2})
    assert again == Completed({"rebuilt": 3, "moved": 0})
    with admin.connection() as c:
        row = c.execute(
            "select units, orders from app.product_sales_counters where shop_id = %s and listing_id = 777",  # noqa: E501
            [shop],
        ).fetchone()
    assert row == (4, 2)  # two paid orders of two units; the cancelled one counts nothing


@pytest.mark.ac("AC-5.11.5")
async def test_counters__a_move_below_zero__clamped_at_zero_and_logged(
    admin: Database, worker_db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """With the ledger a counter can only go below zero through drift or a bug: it stays at
    zero (the check constraint) and the clamp is logged, never silent (sage, #90)."""
    receipt = order(53_201)
    tenant, integration = await stored(admin, worker_db, receipt)
    store = PostgresOrderAutomationStore(worker_db)
    shop = integration.shop_id
    with tenant_scope(tenant.organization_id):
        await store.recount(tenant.organization_id, shop, receipt.receipt_id,
                            execution_id=uuid.uuid4(), now=NOW)  # fmt: skip
    with admin.connection() as c:  # drift: the counter lost the order's units
        c.execute(
            "update app.product_sales_counters set units = 1 where shop_id = %s and listing_id = 777",  # noqa: E501
            [shop],
        )
    cancelled = replace(
        receipt, status="canceled", updated_at=receipt.updated_at + timedelta(hours=1)
    )
    with tenant_scope(tenant.organization_id):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [cancelled],
            context=IngestContext(IngestSource.WEBHOOK, correlation_id="c"), now=NOW,
        )  # fmt: skip
        with caplog.at_level(logging.WARNING, logger="htco_persistence.order_automations"):
            await store.recount(tenant.organization_id, shop, receipt.receipt_id,
                                execution_id=uuid.uuid4(), now=NOW)  # fmt: skip

    with admin.connection() as c:
        row = c.execute(
            "select units, orders from app.product_sales_counters where shop_id = %s and listing_id = 777",  # noqa: E501
            [shop],
        ).fetchone()
    assert row == (0, 0)
    clamps = [r for r in caplog.records if r.getMessage().startswith("product_sales.clamped")]
    assert len(clamps) == 1
    assert clamps[0].levelno == logging.WARNING
