"""The WAITING state of an automation execution (ADR-0019, slice 5.0 alignment of #58)."""

import uuid

import pytest

from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, ExecutionState, TriggerKind
from htco_domain.state_machine import IllegalTransition


def running() -> AutomationExecution:
    return AutomationExecution(
        id=uuid.uuid7(),
        automation_id="etsy-order-intake",
        automation_version=1,
        idempotency_key="k",
        trigger=TriggerKind.EVENT,
        actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"),
        state=ExecutionState.PENDING,
        attempts=0,
        correlation_id="c",
    ).transition(ExecutionState.RUNNING)


@pytest.mark.parametrize("target", [ExecutionState.RETRY_SCHEDULED, ExecutionState.CANCELLED])
def test_waiting__resumes_or_ends(target: ExecutionState) -> None:
    waiting = running().transition(ExecutionState.WAITING)

    assert waiting.state is ExecutionState.WAITING
    assert not waiting.is_terminal
    assert waiting.transition(target).state is target


@pytest.mark.parametrize(
    "target", [ExecutionState.SUCCEEDED, ExecutionState.RUNNING, ExecutionState.FAILED]
)
def test_waiting__anything_else__is_rejected(target: ExecutionState) -> None:
    with pytest.raises(IllegalTransition):
        running().transition(ExecutionState.WAITING).transition(target)


def test_only_a_running_execution__waits() -> None:
    pending = running().transition(ExecutionState.SUCCEEDED)
    with pytest.raises(IllegalTransition):
        pending.transition(ExecutionState.WAITING)
