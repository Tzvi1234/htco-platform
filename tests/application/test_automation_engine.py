"""The workflow execution engine: deterministic, idempotent, authorized (Scenarios G and H).

These cases ran against the first in-memory `AutomationEngine`, retired with slice 5.11 (audit
P2-5): they now run against the durable engine (slice 5.10) with the in-memory execution store,
the engine every deployed automation uses."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from htco_application.automation.durable import (
    Completed,
    DurableAutomationEngine,
    ExecutionNotFoundError,
    HandlerContext,
    HandlerOutcome,
    NotDeadLetteredError,
)
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.execution.gateway import UnavailableGateway
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import (
    AUTOMATION_SPEC_SECTIONS,
    AutomationExecution,
    ExecutionState,
    TriggerKind,
)
from tests.application.workforce_fixtures import (
    advisor_document,
    automation_document,
    employee_document,
    tool_document,
)
from tests.automation_fakes import MemoryExecutionStore

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("3c000000-0000-0000-0000-000000000000")
WORKER = ActorRef(ActorKind.SYSTEM, "agent-worker:w1")
ADMIN = ActorRef(ActorKind.USER, "3c100000-0000-0000-0000-000000000001")


class OrderBook:
    """A stand-in for the CRM: orders by receipt, and unit counters per product."""

    def __init__(self, failures: int = 0) -> None:
        self.orders: dict[str, Mapping[str, object]] = {}
        self.units_sold: dict[str, int] = {}
        self._failures = failures

    async def ingest(self, context: HandlerContext) -> HandlerOutcome:
        payload = context.payload
        if self._failures:
            self._failures -= 1
            raise ConnectionError("CRM write failed")
        receipt = str(payload["receipt_id"])
        self.orders[receipt] = payload
        product = str(payload["product_id"])
        self.units_sold[product] = self.units_sold.get(product, 0) + int(str(payload["quantity"]))
        return Completed({"order": receipt})


ETSY = ActorRef(ActorKind.SYSTEM, "etsy-webhook")


def setup(
    book: OrderBook, **automation_changes: Any
) -> tuple[DurableAutomationEngine, MemoryExecutionStore]:
    registry = Registry.from_documents(
        employees=[
            employee_document("eitan", allowed_automations=["etsy-order-intake"]),
            employee_document("shon"),
            employee_document("dana", readiness="draft", allowed_automations=["etsy-order-intake"]),
        ],
        tools=[tool_document()],
        automations=[automation_document(business_owner="eitan", **automation_changes)],
        advisors=[advisor_document()],
    )
    store = MemoryExecutionStore()
    engine = DurableAutomationEngine(
        lambda: registry,
        store,
        {"etsy-order-intake": book.ingest},
        clock=lambda: NOW,
        jitter=lambda: 1.0,
        gateway=UnavailableGateway(),
    )
    return engine, store


def paid_order(actor: ActorRef = ETSY, trigger: TriggerKind = TriggerKind.EVENT) -> TriggerRequest:
    return TriggerRequest(
        automation_id="etsy-order-intake",
        trigger=trigger,
        actor=actor,
        idempotency_key="etsy:shop1:receipt3412987711:paid",
        payload={"receipt_id": "3412987711", "product_id": "p-1", "quantity": 2},
        correlation_id="corr-1",
        organization_id=ORG,
    )


async def run(
    engine: DurableAutomationEngine, execution: AutomationExecution
) -> AutomationExecution:
    return await engine.execute(execution.id, worker=WORKER)


@pytest.mark.ac("SCN-G")
async def test_replaying_the_same_paid_order__creates_exactly_one_order_and_one_count() -> None:
    book = OrderBook()
    engine, store = setup(book)

    executions = [engine.trigger(paid_order()) for _ in range(3)]
    done = [await run(engine, e) for e in executions]

    assert len({e.id for e in executions}) == 1
    assert done[0].state is ExecutionState.SUCCEEDED
    assert list(book.orders) == ["3412987711"]
    assert book.units_sold == {"p-1": 2}
    assert len(store.rows) == 1


async def test_failure__is_retried_on_the_next_trigger_and_then_succeeds() -> None:
    book = OrderBook(failures=1)
    engine, _ = setup(book)
    execution = engine.trigger(paid_order())

    first = await run(engine, execution)
    second = await run(engine, execution)

    assert first.state is ExecutionState.RETRY_SCHEDULED
    assert (second.state, second.attempts) == (ExecutionState.SUCCEEDED, 2)
    assert book.units_sold == {"p-1": 2}


async def test_failures_beyond_the_retry_policy__are_dead_lettered() -> None:
    book = OrderBook(failures=5)
    engine, _ = setup(
        book, retry={"max_attempts": 2, "initial_backoff_seconds": 1, "max_backoff_seconds": 1}
    )
    execution = engine.trigger(paid_order())

    await run(engine, execution)
    last = await run(engine, execution)

    assert last.state is ExecutionState.DEAD_LETTERED
    assert book.orders == {}


async def test_retry__of_an_unknown_or_finished_execution__is_refused() -> None:
    engine, _ = setup(OrderBook())
    done = await run(engine, engine.trigger(paid_order()))

    with pytest.raises(NotDeadLetteredError):
        engine.retry_dead_lettered(ORG, done.id, actor=ADMIN, correlation_id="c")
    with pytest.raises(ExecutionNotFoundError):
        engine.retry_dead_lettered(ORG, uuid.uuid7(), actor=ADMIN, correlation_id="c")


@pytest.mark.ac("SCN-H")
async def test_employee_with_the_automation_granted__may_trigger_it_as_a_tool() -> None:
    engine, _ = setup(OrderBook())

    execution = engine.trigger(
        paid_order(ActorRef(ActorKind.EMPLOYEE, "eitan"), TriggerKind.AGENT_TOOL)
    )

    assert (await run(engine, execution)).state is ExecutionState.SUCCEEDED


@pytest.mark.ac("SCN-H")
@pytest.mark.parametrize(
    ("actor", "message"),
    [
        (ActorRef(ActorKind.EMPLOYEE, "shon"), "not granted"),
        (ActorRef(ActorKind.EMPLOYEE, "dana"), "not active"),
        (ActorRef(ActorKind.EMPLOYEE, "mallory"), "unknown employee"),
    ],
)
async def test_employee_outside_its_automation_policy__is_refused(
    actor: ActorRef, message: str
) -> None:
    engine, store = setup(OrderBook())

    with pytest.raises(AutomationRefused, match=message):
        engine.trigger(paid_order(actor, TriggerKind.AGENT_TOOL))
    assert store.rows == {}


async def test_employee_must_use_the_agent_tool_trigger() -> None:
    engine, _ = setup(OrderBook())

    with pytest.raises(AutomationRefused, match="agent_tool"):
        engine.trigger(paid_order(ActorRef(ActorKind.EMPLOYEE, "eitan"), TriggerKind.EVENT))


async def test_trigger_kind_not_allowed_for_the_automation__is_refused() -> None:
    engine, _ = setup(OrderBook())

    with pytest.raises(AutomationRefused, match="user_action"):
        engine.trigger(paid_order(trigger=TriggerKind.USER_ACTION))


async def test_unknown_automation__is_refused() -> None:
    engine, _ = setup(OrderBook())
    request = paid_order()

    with pytest.raises(AutomationRefused, match="unknown automation"):
        engine.trigger(
            TriggerRequest(
                "no-such-flow", request.trigger, request.actor, "k", {}, "c",
                organization_id=ORG,
            )
        )  # fmt: skip


async def test_automation_not_production_ready__is_refused() -> None:
    drafts = {name: {"status": "draft", "text": name} for name in AUTOMATION_SPEC_SECTIONS}
    engine, _ = setup(OrderBook(), readiness="draft", specification=drafts)

    with pytest.raises(AutomationRefused, match="not active"):
        engine.trigger(paid_order())


async def test_automation_without_a_handler__is_refused() -> None:
    registry = Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[automation_document()],
        advisors=[advisor_document()],
    )
    engine = DurableAutomationEngine(
        lambda: registry, MemoryExecutionStore(), {}, clock=lambda: NOW, jitter=lambda: 1.0,
        gateway=UnavailableGateway(),
    )  # fmt: skip

    with pytest.raises(AutomationRefused, match="no handler"):
        engine.trigger(paid_order())
