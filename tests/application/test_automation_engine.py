"""The workflow execution engine: deterministic, idempotent, authorized (Scenarios G and H)."""

import uuid
from collections.abc import Mapping
from typing import Any, override

import pytest

from htco_application.automation.engine import (
    AutomationEngine,
    AutomationRefused,
    ExecutionStore,
    TriggerRequest,
)
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


class InMemoryExecutionStore(ExecutionStore):
    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], AutomationExecution] = {}

    @override
    def find(self, automation_id: str, idempotency_key: str) -> AutomationExecution | None:
        return self.saved.get((automation_id, idempotency_key))

    @override
    def save(self, execution: AutomationExecution) -> None:
        self.saved[(execution.automation_id, execution.idempotency_key)] = execution


class OrderBook:
    """A stand-in for the CRM: orders by receipt, and unit counters per product."""

    def __init__(self, failures: int = 0) -> None:
        self.orders: dict[str, Mapping[str, object]] = {}
        self.units_sold: dict[str, int] = {}
        self._failures = failures

    def ingest(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        if self._failures:
            self._failures -= 1
            raise ConnectionError("CRM write failed")
        receipt = str(payload["receipt_id"])
        self.orders[receipt] = payload
        product = str(payload["product_id"])
        self.units_sold[product] = self.units_sold.get(product, 0) + int(str(payload["quantity"]))
        return {"order": receipt}


ETSY = ActorRef(ActorKind.SYSTEM, "etsy-webhook")


def setup(
    book: OrderBook, **automation_changes: Any
) -> tuple[AutomationEngine, InMemoryExecutionStore]:
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
    store = InMemoryExecutionStore()
    engine = AutomationEngine(
        registry, store, handlers={"etsy-order-intake": book.ingest}, new_id=uuid.uuid7
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
    )


@pytest.mark.ac("SCN-G")
def test_replaying_the_same_paid_order__creates_exactly_one_order_and_one_count() -> None:
    book = OrderBook()
    engine, store = setup(book)

    executions = [engine.trigger(paid_order()) for _ in range(3)]

    assert len({e.id for e in executions}) == 1
    assert executions[0].state is ExecutionState.SUCCEEDED
    assert list(book.orders) == ["3412987711"]
    assert book.units_sold == {"p-1": 2}
    assert len(store.saved) == 1


def test_failure__is_retried_on_the_next_trigger_and_then_succeeds() -> None:
    book = OrderBook(failures=1)
    engine, _ = setup(book)

    first = engine.trigger(paid_order())
    second = engine.retry(first.automation_id, first.idempotency_key)

    assert first.state is ExecutionState.RETRY_SCHEDULED
    assert (second.state, second.attempts) == (ExecutionState.SUCCEEDED, 2)
    assert book.units_sold == {"p-1": 2}


def test_failures_beyond_the_retry_policy__are_dead_lettered() -> None:
    book = OrderBook(failures=5)
    engine, _ = setup(
        book, retry={"max_attempts": 2, "initial_backoff_seconds": 1, "max_backoff_seconds": 1}
    )

    first = engine.trigger(paid_order())
    last = engine.retry(first.automation_id, first.idempotency_key)

    assert last.state is ExecutionState.DEAD_LETTERED
    assert book.orders == {}


def test_retry__of_an_unknown_or_finished_execution__is_refused() -> None:
    engine, _ = setup(OrderBook())
    done = engine.trigger(paid_order())

    with pytest.raises(AutomationRefused, match="nothing to retry"):
        engine.retry(done.automation_id, done.idempotency_key)
    with pytest.raises(AutomationRefused, match="nothing to retry"):
        engine.retry("etsy-order-intake", "unknown-key")


@pytest.mark.ac("SCN-H")
def test_employee_with_the_automation_granted__may_trigger_it_as_a_tool() -> None:
    engine, _ = setup(OrderBook())

    execution = engine.trigger(
        paid_order(ActorRef(ActorKind.EMPLOYEE, "eitan"), TriggerKind.AGENT_TOOL)
    )

    assert execution.state is ExecutionState.SUCCEEDED


@pytest.mark.ac("SCN-H")
@pytest.mark.parametrize(
    ("actor", "message"),
    [
        (ActorRef(ActorKind.EMPLOYEE, "shon"), "not granted"),
        (ActorRef(ActorKind.EMPLOYEE, "dana"), "not active"),
        (ActorRef(ActorKind.EMPLOYEE, "mallory"), "unknown employee"),
    ],
)
def test_employee_outside_its_automation_policy__is_refused(actor: ActorRef, message: str) -> None:
    engine, store = setup(OrderBook())

    with pytest.raises(AutomationRefused, match=message):
        engine.trigger(paid_order(actor, TriggerKind.AGENT_TOOL))
    assert store.saved == {}


def test_employee_must_use_the_agent_tool_trigger() -> None:
    engine, _ = setup(OrderBook())

    with pytest.raises(AutomationRefused, match="agent_tool"):
        engine.trigger(paid_order(ActorRef(ActorKind.EMPLOYEE, "eitan"), TriggerKind.EVENT))


def test_trigger_kind_not_allowed_for_the_automation__is_refused() -> None:
    engine, _ = setup(OrderBook())

    with pytest.raises(AutomationRefused, match="user_action"):
        engine.trigger(paid_order(trigger=TriggerKind.USER_ACTION))


def test_unknown_automation__is_refused() -> None:
    engine, _ = setup(OrderBook())
    request = paid_order()

    with pytest.raises(AutomationRefused, match="unknown automation"):
        engine.trigger(TriggerRequest("no-such-flow", request.trigger, request.actor, "k", {}, "c"))


def test_automation_not_production_ready__is_refused() -> None:
    drafts = {name: {"status": "draft", "text": name} for name in AUTOMATION_SPEC_SECTIONS}
    engine, _ = setup(OrderBook(), readiness="draft", specification=drafts)

    with pytest.raises(AutomationRefused, match="not active"):
        engine.trigger(paid_order())


def test_automation_without_a_handler__is_refused() -> None:
    registry = Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[automation_document()],
        advisors=[advisor_document()],
    )
    engine = AutomationEngine(registry, InMemoryExecutionStore(), handlers={}, new_id=uuid.uuid7)

    with pytest.raises(AutomationRefused, match="no handler"):
        engine.trigger(paid_order())
