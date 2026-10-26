"""The API's automation engine creates executions for the worker and never runs one (5.10,
5.11): it knows which automations the worker deploys (`WORKER_AUTOMATIONS`), so a user's trigger
of a ready one (the counters rebuild, activation step 2b) is accepted, and a draft or undeployed
one is refused before anything is written."""

import uuid
from typing import Any

import pytest

from htco_api.main import api_engine, automation_services
from htco_application.automation.deployed import WORKER_AUTOMATIONS
from htco_application.automation.durable import DurableAutomationEngine, HandlerContext
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.jobs import JobError
from htco_application.order_automations import SALES_COUNTERS
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import ExecutionState, TriggerKind
from htco_persistence.database import Database
from tests.application.workforce_fixtures import (
    advisor_document,
    employee_document,
    tool_document,
)
from tests.automation_fakes import MemoryExecutionStore, engine_document

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("15700000-0000-0000-0000-000000000001")
OWNER = ActorRef(ActorKind.USER, "15700000-0000-0000-0000-000000000009")


def registry(automation_id: str, **changes: Any) -> Registry:
    return Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[engine_document(automation_id, **changes)],
        advisors=[advisor_document()],
    )


def rebuild(automation_id: str = SALES_COUNTERS) -> TriggerRequest:
    return TriggerRequest(
        automation_id=automation_id, trigger=TriggerKind.USER_ACTION, actor=OWNER,
        idempotency_key=f"user:{ORG}:rebuild-1", payload={"rebuild": True},
        correlation_id="corr-rebuild", organization_id=ORG,
    )  # fmt: skip


def test_a_ready_automation_the_worker_deploys__accepted_for_the_worker() -> None:
    store = MemoryExecutionStore()
    engine = api_engine(lambda: registry(SALES_COUNTERS), store)

    execution = engine.trigger(rebuild())

    assert execution.state is ExecutionState.PENDING
    assert len(store.rows) == 1
    assert len(store.jobs) == 1  # the worker's job; the API never runs it


@pytest.mark.parametrize(
    ("automation_id", "changes"),
    [(SALES_COUNTERS, {"readiness": "draft"}), ("an-automation-not-deployed", {})],
    ids=["draft", "not-deployed"],
)
def test_a_draft_or_undeployed_automation__refused_before_anything_is_written(
    automation_id: str, changes: dict[str, Any]
) -> None:
    store = MemoryExecutionStore()
    engine = api_engine(lambda: registry(automation_id, **changes), store)

    with pytest.raises(AutomationRefused):
        engine.trigger(rebuild(automation_id))

    assert store.rows == {}
    assert store.jobs == []


async def test_the_api_placeholders__never_run_a_handler() -> None:
    store = MemoryExecutionStore()
    engine = api_engine(lambda: registry(SALES_COUNTERS), store)
    handler = engine._handlers[SALES_COUNTERS]
    context = HandlerContext(
        execution_id=uuid.uuid7(), automation_id=SALES_COUNTERS, actor=OWNER,
        organization_id=ORG, shop_id=None, correlation_id="c", causation_id=None, attempt=1,
        payload={}, gateway=None,  # type: ignore[arg-type]
    )  # fmt: skip

    with pytest.raises(JobError, match="worker"):
        await handler(context)


def test_automation_services__enabled__wires_the_api_engine() -> None:
    services = automation_services(Database("postgresql://htco@db.invalid/htco"), enabled=True)

    assert isinstance(services.engine, DurableAutomationEngine)
    assert set(services.engine._handlers) == WORKER_AUTOMATIONS
