"""Etsy Order Intake's handler (slice 5.4) through the real Action Gateway and API channel, with
in-memory stores: the receipt re-read, the history run, deferrals and refusals, the API
process's sync requests, and the worker's sync trigger. All values are invented."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import ORDER_INTAKE
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import (
    ActionGateway,
    ActionOutcome,
    ActionRequest,
    ActionResult,
    Channel,
)
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob, JobError
from htco_application.marketplace import MarketplaceRateLimitedError
from htco_application.order_ingest import INGESTED, IngestSource
from htco_application.order_intake import (
    ORDER_SYNC_JOB,
    RECEIPTS_READ,
    EngineSyncTrigger,
    IntakeActionError,
    OrderIntake,
    QueuedSyncRequests,
)
from htco_application.shop_integrations import EtsyConnection, OAuthTokens
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, ExecutionState, TriggerKind
from htco_domain.integrations import IntegrationStatus
from htco_domain.orders import OrderState
from tests.application.test_action_gateway import ORG, SHOP, office
from tests.application.test_execution_router import ETSY_SHOP, EtsyShops, FakeMarketplace
from tests.etsy_fakes import Vault
from tests.execution_fakes import Approvals, Capabilities, MemoryActionStore, Principals, Shops
from tests.execution_fakes import real_catalog as catalog_of
from tests.job_fakes import MemoryJobQueue
from tests.log_capture import capture_logs
from tests.order_intake_fakes import FAKE_NAME, NOW, MemoryIntakeStore, paid_receipt, shipped

pytestmark = pytest.mark.anyio

RECEIPT = 4000000001


def _vault(status: IntegrationStatus = IntegrationStatus.CONNECTED) -> Vault:
    vault = Vault()
    vault.add(
        EtsyConnection(
            organization_id=ORG,
            shop_id=SHOP,
            etsy_shop_id=ETSY_SHOP,
            etsy_user_id=12345678,
            scopes=frozenset({"shops_r", "transactions_r", "listings_r"}),
            tokens=OAuthTokens(
                access_token="12345678.fake-access-token",  # noqa: S106 - fake
                refresh_token="12345678.fake-refresh-token",  # noqa: S106 - fake
                access_expires_at=NOW + timedelta(hours=1),
                refresh_expires_at=NOW + timedelta(days=90),
            ),
        )
    )
    vault.rows[SHOP] = replace(vault.rows[SHOP], status=status)
    return vault


@dataclass
class Intake:
    marketplace: FakeMarketplace = field(default_factory=FakeMarketplace)
    store: MemoryIntakeStore = field(default_factory=MemoryIntakeStore)
    vault: Vault = field(default_factory=_vault)
    action_store: MemoryActionStore = field(default_factory=MemoryActionStore)
    waits: list[float] = field(default_factory=list)

    @property
    def gateway(self) -> ActionGatewayService:
        registry = office()
        return ActionGatewayService(
            GatewayParts(
                catalog=catalog_of(registry),
                registry=lambda: registry,
                store=self.action_store,
                principals=Principals({}),
                shops=Shops({}),
                approvals=Approvals(),
                adapters={Channel.API: EtsyApiChannel(self.marketplace, EtsyShops())},
                capabilities=Capabilities({(SHOP, "receipts_read")}),
                clock=lambda: NOW,
            )
        )

    def context(
        self, payload: dict[str, Any], *, gateway: ActionGateway | None = None, attempt: int = 1
    ) -> HandlerContext:
        return HandlerContext(
            execution_id=uuid.uuid7(),
            automation_id=ORDER_INTAKE,
            actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"),
            organization_id=ORG,
            shop_id=SHOP,
            correlation_id="etsy-webhook:msg_1",
            causation_id="job:1",
            attempt=attempt,
            payload=payload,
            gateway=gateway or self.gateway,
        )

    @property
    def handler(self) -> OrderIntake:
        async def sleep(seconds: float) -> None:
            self.waits.append(seconds)

        return OrderIntake(store=self.store, shops=self.vault, clock=lambda: NOW, sleep=sleep)


@pytest.mark.ac("AC-5.4.6")
async def test_receipt__reread_through_the_gateway_and_ingested_with_its_buyer_row() -> None:
    intake = Intake(FakeMarketplace(order_receipts=(paid_receipt(RECEIPT),)))

    outcome = await intake.handler(
        intake.context({"receipt_id": RECEIPT, "event_type": "order.paid"})
    )

    assert outcome == Completed(
        {"receipt_id": RECEIPT, "written": True, "state": "awaiting_shipment"}
    )
    assert intake.marketplace.calls == [("get_order_receipt", ETSY_SHOP, {"receipt_id": RECEIPT})]
    stored = intake.store.orders[(SHOP, RECEIPT)]
    assert stored.buyer_name == FAKE_NAME  # the buyer row is kept (ADR-0018)
    [row] = intake.action_store.rows.values()
    assert row.request.action == RECEIPTS_READ
    assert row.request.actor == ActorRef(ActorKind.AUTOMATION, ORDER_INTAKE)
    assert row.request.input == {"order_history": True, "receipt_id": RECEIPT}
    # An R1 output: returned to the handler, never stored with the action.
    assert all(not step.store_output for step in row.steps if step.result.output)
    assert {t[2] for t in intake.store.transitions} == {IngestSource.WEBHOOK.value}


@pytest.mark.ac("AC-5.4.1")
async def test_receipt__the_same_event_ten_times__one_order_and_one_ingested_event() -> None:
    intake = Intake(FakeMarketplace(order_receipts=(paid_receipt(RECEIPT),)))

    for attempt in range(10):
        await intake.handler(
            intake.context({"receipt_id": RECEIPT, "event_type": "order.paid"}, attempt=attempt)
        )

    assert list(intake.store.orders) == [(SHOP, RECEIPT)]
    assert sum(line.quantity for line in intake.store.orders[(SHOP, RECEIPT)].lines) == 3
    ingested = [e for e in intake.store.events.values() if e.payload["event_type"] == INGESTED]
    assert len(ingested) == 1
    assert len(intake.store.transitions) == 3  # paid, ingested, awaiting shipment: once


async def test_receipt__order_delivered_marks_a_shipped_order_delivered() -> None:
    intake = Intake(FakeMarketplace(order_receipts=(shipped(paid_receipt(RECEIPT), minutes=9),)))

    outcome = await intake.handler(
        intake.context({"receipt_id": RECEIPT, "event_type": "order.delivered"})
    )

    assert isinstance(outcome, Completed)
    assert outcome.output["state"] == OrderState.DELIVERED.value


async def test_receipt__a_disconnected_shop_is_skipped_without_a_read() -> None:
    intake = Intake(vault=_vault(IntegrationStatus.NEEDS_REAUTH))

    outcome = await intake.handler(intake.context({"receipt_id": RECEIPT}))

    assert outcome == Completed({"skipped": "not_connected"})
    assert intake.marketplace.calls == []


@dataclass
class ScriptedGateway:
    results: list[ActionResult]
    requests: list[ActionRequest] = field(default_factory=list)

    async def submit(self, request: ActionRequest) -> ActionResult:
        self.requests.append(request)
        return self.results.pop(0)


def _result(outcome: ActionOutcome, **changes: Any) -> ActionResult:
    return ActionResult(execution_id=uuid.uuid7(), outcome=outcome, attempts=0, **changes)


async def test_receipt__a_denied_read_fails_the_run_with_a_safe_message() -> None:
    intake = Intake()
    gateway = ScriptedGateway([_result(ActionOutcome.DENIED, error_code="forbidden")])

    with pytest.raises(IntakeActionError, match="denied: forbidden"):
        await intake.handler(intake.context({"receipt_id": RECEIPT}, gateway=gateway))


async def test_receipt__a_deferred_read_raises_a_rate_limit_with_the_wait() -> None:
    intake = Intake()
    gateway = ScriptedGateway(
        [
            _result(
                ActionOutcome.DEFERRED,
                error_code="rate_limited",
                retry_at=NOW + timedelta(seconds=7),
            )
        ]
    )

    with pytest.raises(MarketplaceRateLimitedError) as raised:
        await intake.handler(intake.context({"receipt_id": RECEIPT}, gateway=gateway))
    assert raised.value.retry_after_seconds == 7


async def test_history__backfill_then_30_days_through_the_gateway() -> None:
    receipts = tuple(paid_receipt(RECEIPT + n) for n in range(3))
    intake = Intake(FakeMarketplace(order_receipts=receipts))

    outcome = await intake.handler(intake.context({"mode": "history", "reason": "stale"}))

    assert outcome == Completed({"mode": "history", "orders_stored": 3})
    reads = [call for call in intake.marketplace.calls if call[0] == "list_order_receipts"]
    assert len(reads) == 2  # one backfill page, one 30-day page
    assert reads[1][2]["min_last_modified"] == NOW - timedelta(days=30)
    sources = [ctx.source for ctx in intake.store.contexts]
    assert sources == [IngestSource.BACKFILL, IngestSource.SYNC]
    requests = [row.request for row in intake.action_store.rows.values()]
    assert all(r.input["order_history"] is True for r in requests)


async def test_history__a_lock_held_elsewhere__skipped() -> None:
    intake = Intake()
    intake.store.held.add(SHOP)

    outcome = await intake.handler(intake.context({"mode": "history"}))

    assert outcome == Completed({"mode": "history", "skipped": "locked"})


@pytest.mark.parametrize("payload", [{}, {"mode": "other"}, {"receipt_id": True}])
async def test_handler__a_payload_without_a_receipt_or_mode_fails(payload: dict[str, Any]) -> None:
    intake = Intake()
    with pytest.raises(JobError):
        await intake.handler(intake.context(payload))


async def test_channel__the_order_history_form_is_the_automations_alone() -> None:
    intake = Intake(FakeMarketplace(order_receipts=(paid_receipt(RECEIPT),)))
    request = ActionRequest(
        action=RECEIPTS_READ,
        actor=ActorRef(ActorKind.EMPLOYEE, "eitan"),
        organization_id=ORG,
        shop_id=SHOP,
        idempotency_key="emp:1",
        correlation_id="c",
        input={"order_history": True, "receipt_id": RECEIPT},
    )

    result = await intake.gateway.submit(request)

    assert result.outcome is ActionOutcome.FAILED
    assert intake.marketplace.calls == []


# --- Requests and the worker's trigger --------------------------------------------------------


async def test_requests__the_api_enqueues_one_job_per_period_and_never_runs_the_sync() -> None:
    queue = MemoryJobQueue()
    requests = QueuedSyncRequests(queue)
    integration = _vault().rows[SHOP]

    for minute in range(3):
        await requests.request(integration, now=NOW + timedelta(minutes=minute), force=False)
    await requests.request(integration, now=NOW, force=True)
    await requests.request(integration, now=NOW + timedelta(seconds=30), force=True)

    jobs = list(queue.jobs.values())
    assert {j.spec.kind for j in jobs} == {ORDER_SYNC_JOB}
    assert sorted(str(j.spec.payload["reason"]) for j in jobs) == ["manual", "stale"]
    assert all(j.spec.shop_id == SHOP for j in jobs)


@dataclass
class Engine:
    refuse: bool = False
    requests: list[TriggerRequest] = field(default_factory=list)

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        self.requests.append(request)
        if self.refuse:
            raise AutomationRefused("etsy-order-intake is not active (draft)")
        return AutomationExecution(
            id=uuid.uuid7(), automation_id=request.automation_id, automation_version=1,
            idempotency_key=request.idempotency_key, trigger=request.trigger,
            actor=request.actor, state=ExecutionState.PENDING, attempts=0,
            correlation_id=request.correlation_id, organization_id=request.organization_id,
            shop_id=request.shop_id,
        )  # fmt: skip


def _sync_job() -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid7(), kind=ORDER_SYNC_JOB, idempotency_key=f"{ORDER_SYNC_JOB}:{SHOP}:stale:x",
        payload={"reason": "stale"}, organization_id=ORG, shop_id=SHOP, lock_key=None,
        attempts=1, retry=DEFAULT_RETRY, lease_expires_at=NOW, correlation_id="order-sync:1",
    )  # fmt: skip


async def test_sync_trigger__starts_the_history_run_of_order_intake() -> None:
    engine = Engine()

    assert await EngineSyncTrigger(engine).run(_sync_job())

    [request] = engine.requests
    assert request.automation_id == ORDER_INTAKE
    assert request.trigger is TriggerKind.EVENT
    assert request.payload == {"mode": "history", "reason": "stale"}
    assert (request.organization_id, request.shop_id) == (ORG, SHOP)


async def test_sync_trigger__a_refusal_is_logged_not_retried() -> None:
    with capture_logs() as logs:
        assert not await EngineSyncTrigger(Engine(refuse=True)).run(_sync_job())
    assert any(line["event"].startswith("etsy.order_sync_not_started") for line in logs.lines())


async def test_sync_trigger__needs_its_tenant() -> None:
    job = replace(_sync_job(), organization_id=None, shop_id=None)
    with pytest.raises(JobError):
        await EngineSyncTrigger(Engine()).run(job)
