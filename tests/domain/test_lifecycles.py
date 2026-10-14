"""Lifecycles as explicit state machines: automations, tasks, optimization proposals."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import (
    AUTOMATION_SPEC_SECTIONS,
    AutomationDefinition,
    AutomationExecution,
    ExecutionState,
    RetryPolicy,
    TriggerKind,
)
from htco_domain.improvement import OptimizationProposal, ProposalState
from htco_domain.state_machine import IllegalTransition
from htco_domain.tasks import BusinessTask, TaskState
from htco_domain.tooling import SideEffect
from htco_domain.workforce import Readiness, SectionStatus, SpecSection

ORG = uuid.UUID("00000000-0000-7000-8000-0000000000a0")
WORKER = ActorRef(ActorKind.AUTOMATION, "etsy-order-intake")
OMER = ActorRef(ActorKind.EMPLOYEE, "omer")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# --- actors -------------------------------------------------------------------------------


def test_actor__blank_id__is_rejected() -> None:
    with pytest.raises(ValueError, match="actor id"):
        ActorRef(ActorKind.EMPLOYEE, " ")


def test_actor__renders_as_kind_and_id() -> None:
    assert str(OMER) == "employee:omer"


# --- automations ----------------------------------------------------------------------------


def _automation_sections() -> dict[str, SpecSection]:
    return {name: SpecSection(SectionStatus.DRAFT, name) for name in AUTOMATION_SPEC_SECTIONS}


DEFINITION = AutomationDefinition(
    id="etsy-order-intake",
    name="Etsy Order Intake",
    version=1,
    business_owner="eitan",
    primary_trigger=TriggerKind.EVENT,
    callable_from=frozenset({TriggerKind.EVENT, TriggerKind.SCHEDULE}),
    retry=RetryPolicy(max_attempts=5, initial_backoff=timedelta(seconds=5)),
    timeout=timedelta(seconds=30),
    side_effect=SideEffect.INTERNAL,
    approval_required=False,
    ai_steps=(),
    readiness=Readiness.DRAFT,
    activation_phase=5,
    specification=_automation_sections(),
)


def test_automation_spec__has_eighteen_sections() -> None:
    assert len(AUTOMATION_SPEC_SECTIONS) == 18


def test_automation__primary_trigger_must_be_callable() -> None:
    with pytest.raises(ValueError, match="primary trigger"):
        replace(DEFINITION, callable_from=frozenset({TriggerKind.SCHEDULE}))


@pytest.mark.parametrize("bad_id", ["Etsy", "etsy order", "e"])
def test_automation__invalid_id__is_rejected(bad_id: str) -> None:
    with pytest.raises(ValueError, match="automation id"):
        replace(DEFINITION, id=bad_id)


def test_automation__missing_section__is_rejected() -> None:
    sections = _automation_sections()
    del sections["idempotency"]

    with pytest.raises(ValueError, match="idempotency"):
        replace(DEFINITION, specification=sections)


def test_automation__ready_with_draft_sections__is_rejected() -> None:
    with pytest.raises(ValueError, match="draft"):
        replace(DEFINITION, readiness=Readiness.READY)


def test_automation__activation_phase__must_be_a_plan_phase() -> None:
    with pytest.raises(ValueError, match="phase"):
        replace(DEFINITION, activation_phase=-1)


def test_automation__version_and_timeout__must_be_positive() -> None:
    with pytest.raises(ValueError, match="version"):
        replace(DEFINITION, version=0)
    with pytest.raises(ValueError, match="timeout"):
        replace(DEFINITION, timeout=timedelta(0))


def test_retry_policy__needs_an_attempt_and_a_sane_backoff() -> None:
    with pytest.raises(ValueError, match="attempt"):
        RetryPolicy(max_attempts=0, initial_backoff=timedelta(seconds=1))
    with pytest.raises(ValueError, match="backoff"):
        RetryPolicy(max_attempts=1, initial_backoff=timedelta(seconds=-1))


def test_retry_policy__backoff_doubles_up_to_the_cap() -> None:
    policy = RetryPolicy(
        max_attempts=6, initial_backoff=timedelta(seconds=5), max_backoff=timedelta(seconds=30)
    )

    assert [policy.backoff(attempt).seconds for attempt in range(1, 6)] == [5, 10, 20, 30, 30]


def _execution() -> AutomationExecution:
    return AutomationExecution(
        id=uuid.uuid7(),
        automation_id=DEFINITION.id,
        automation_version=1,
        idempotency_key="etsy:shop1:receipt1:paid",
        trigger=TriggerKind.EVENT,
        actor=WORKER,
        state=ExecutionState.PENDING,
        attempts=0,
        correlation_id="corr-1",
    )


def test_execution__happy_path__counts_attempts() -> None:
    running = _execution().transition(ExecutionState.RUNNING)
    done = running.transition(ExecutionState.SUCCEEDED)

    assert (running.attempts, done.state) == (1, ExecutionState.SUCCEEDED)
    assert done.is_terminal


def test_execution__retry_then_dead_letter() -> None:
    failed = _execution().transition(ExecutionState.RUNNING).transition(ExecutionState.FAILED)
    retried = failed.transition(ExecutionState.RETRY_SCHEDULED).transition(ExecutionState.RUNNING)

    assert retried.attempts == 2
    assert (
        retried.transition(ExecutionState.FAILED)
        .transition(ExecutionState.DEAD_LETTERED)
        .is_terminal
    )


def test_execution__succeeded_cannot_run_again() -> None:
    done = _execution().transition(ExecutionState.RUNNING).transition(ExecutionState.SUCCEEDED)

    with pytest.raises(IllegalTransition, match="succeeded -> running"):
        done.transition(ExecutionState.RUNNING)


def test_execution__blank_idempotency_key__is_rejected() -> None:
    with pytest.raises(ValueError, match="idempotency"):
        replace(_execution(), idempotency_key="")


# --- business tasks -------------------------------------------------------------------------


def _task() -> BusinessTask:
    return BusinessTask(
        id=uuid.uuid7(),
        organization_id=ORG,
        shop_id=None,
        title="Prepare the weekly review",
        assignee=ActorRef(ActorKind.EMPLOYEE, "maya"),
        state=TaskState.CREATED,
        correlation_id="corr-2",
    )


def test_task_states__are_the_thirteen_of_the_prd() -> None:
    assert len(TaskState) == 13


def test_task__moves_through_its_lifecycle() -> None:
    task = _task()
    for state in (
        TaskState.QUEUED,
        TaskState.CLAIMED,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_APPROVAL,
        TaskState.RUNNING,
        TaskState.COMPLETED,
    ):
        task = task.transition(state)

    assert task.state is TaskState.COMPLETED


def test_task__completed__is_final() -> None:
    task = replace(_task(), state=TaskState.COMPLETED)

    with pytest.raises(IllegalTransition):
        task.transition(TaskState.RUNNING)


def test_task__blank_title__is_rejected() -> None:
    with pytest.raises(ValueError, match="title"):
        replace(_task(), title="")


# --- optimization proposals -----------------------------------------------------------------


def _proposal(**changes: object) -> OptimizationProposal:
    proposal = OptimizationProposal(
        id=uuid.uuid7(),
        title="Replace the metadata-to-JSON model call with code",
        detected_by=OMER,
        state=ProposalState.DETECTED,
        problem="An LLM call reformats structured metadata on every listing.",
        deterministic_candidate=True,
        acceptance_criteria=("Same output on the 200-listing fixture", "Cost per listing -20%"),
        rollback_plan="Re-enable the model step behind its feature flag.",
    )
    return replace(proposal, **changes)  # type: ignore[arg-type]


def test_proposal_lifecycle__reaches_verified() -> None:
    proposal = _proposal()
    for state in (
        ProposalState.PROPOSED,
        ProposalState.EVALUATING,
        ProposalState.APPROVED,
        ProposalState.DEVELOPMENT,
        ProposalState.TESTING,
        ProposalState.DEPLOYED,
        ProposalState.MEASURING,
        ProposalState.VERIFIED,
    ):
        proposal = proposal.transition(state)

    assert proposal.state is ProposalState.VERIFIED


def test_proposal__measuring_can_roll_back() -> None:
    proposal = _proposal(state=ProposalState.MEASURING)

    assert proposal.transition(ProposalState.ROLLED_BACK).state is ProposalState.ROLLED_BACK


def test_proposal__cannot_skip_evaluation() -> None:
    with pytest.raises(IllegalTransition):
        _proposal(state=ProposalState.PROPOSED).transition(ProposalState.DEVELOPMENT)


def test_proposal__without_acceptance_criteria__cannot_be_proposed() -> None:
    with pytest.raises(ValueError, match="acceptance criteria"):
        _proposal(acceptance_criteria=()).transition(ProposalState.PROPOSED)


def test_proposal__without_rollback_plan__cannot_be_proposed() -> None:
    with pytest.raises(ValueError, match="rollback"):
        _proposal(rollback_plan=" ").transition(ProposalState.PROPOSED)


# --- activity records -------------------------------------------------------------------------


def _record(**changes: object) -> ActivityRecord:
    record = ActivityRecord(
        id=uuid.uuid7(),
        kind=ActivityKind.AUTOMATION_EXECUTION,
        organization_id=ORG,
        shop_id=None,
        actor=WORKER,
        action="orders.ingest",
        outcome=ActivityOutcome.SUCCEEDED,
        summary="Ingested receipt 3412987711.",
        occurred_at=NOW,
        correlation_id="corr-1",
        causation_id=None,
        task_id=None,
    )
    return replace(record, **changes)  # type: ignore[arg-type]


def test_activity_record__valid() -> None:
    assert _record().actor == WORKER


def test_activity_record__naive_time__is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _record(occurred_at=datetime(2026, 10, 14))  # noqa: DTZ001


def test_activity_record__summary_is_a_short_business_summary() -> None:
    """Records hold business decisions and outcomes, never long reasoning transcripts."""
    with pytest.raises(ValueError, match="summary"):
        _record(summary="x" * 501)
    with pytest.raises(ValueError, match="summary"):
        _record(summary="")


def test_activity_record__needs_an_action_and_a_correlation_id() -> None:
    with pytest.raises(ValueError, match="action"):
        _record(action="")
    with pytest.raises(ValueError, match="correlation"):
        _record(correlation_id="")
