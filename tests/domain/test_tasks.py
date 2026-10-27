"""Business tasks (slice 7.1): one task model with kinds, every transition, completion on
evidence."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from htco_domain.actors import ActorKind, ActorRef
from htco_domain.state_machine import IllegalTransition
from htco_domain.tasks import (
    BusinessTask,
    EvidenceKind,
    MissingEvidenceError,
    TaskEvidence,
    TaskKind,
    TaskState,
)

ORG = uuid.UUID("07100000-0000-7000-8000-0000000000aa")
_S = TaskState

# The expected machine, written out on its own (AC-7.1.1): every pair not listed is illegal.
ALLOWED: dict[TaskState, set[TaskState]] = {
    _S.CREATED: {_S.QUEUED, _S.CANCELLED},
    _S.QUEUED: {_S.CLAIMED, _S.CANCELLED},
    _S.CLAIMED: {_S.RUNNING, _S.QUEUED},
    _S.RUNNING: {
        _S.WAITING_FOR_EXTERNAL, _S.WAITING_FOR_AGENT, _S.WAITING_FOR_APPROVAL, _S.COMPLETED,
        _S.FAILED, _S.RETRY_SCHEDULED, _S.BLOCKED, _S.ESCALATED,
    },
    _S.WAITING_FOR_EXTERNAL: {_S.RUNNING, _S.QUEUED, _S.CANCELLED, _S.ESCALATED},
    _S.WAITING_FOR_AGENT: {_S.RUNNING, _S.QUEUED, _S.CANCELLED, _S.ESCALATED},
    _S.WAITING_FOR_APPROVAL: {_S.RUNNING, _S.QUEUED, _S.CANCELLED, _S.ESCALATED},
    _S.RETRY_SCHEDULED: {_S.QUEUED, _S.CANCELLED},
    _S.BLOCKED: {_S.QUEUED, _S.ESCALATED, _S.CANCELLED},
    _S.FAILED: {_S.RETRY_SCHEDULED, _S.ESCALATED},
    _S.ESCALATED: {_S.QUEUED, _S.CANCELLED},
    _S.COMPLETED: set(),
    _S.CANCELLED: set(),
}  # fmt: skip
EVIDENCE = TaskEvidence(EvidenceKind.EXECUTION, "0190a000-0000-7000-8000-0000000000ee")


def task(state: TaskState = _S.CREATED, /, **changes: object) -> BusinessTask:
    base = BusinessTask(
        id=uuid.uuid7(),
        organization_id=ORG,
        shop_id=None,
        kind=TaskKind.HUMAN_CHANNEL,
        title="Log in to Etsy again",
        assignee=ActorRef(ActorKind.USER, "0190a000-0000-7000-8000-000000000001"),
        state=state,
        idempotency_key="human_channel:execution-1",
        correlation_id="corr-1",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_the_expected_machine__names_every_state() -> None:
    assert set(ALLOWED) == set(TaskState)


@pytest.mark.ac("AC-7.1.1")
@pytest.mark.parametrize(
    ("current", "target"),
    [(c, t) for c in TaskState for t in TaskState if t in ALLOWED[c]],
)
def test_every_allowed_transition__moves_the_task(current: TaskState, target: TaskState) -> None:
    evidence = EVIDENCE if target is _S.COMPLETED else None

    moved = task(current).transition(target, evidence=evidence)

    assert moved.state is target


@pytest.mark.ac("AC-7.1.1")
@pytest.mark.parametrize(
    ("current", "target"),
    [(c, t) for c in TaskState for t in TaskState if t not in ALLOWED[c]],
)
def test_every_illegal_transition__is_rejected(current: TaskState, target: TaskState) -> None:
    with pytest.raises(IllegalTransition):
        task(current).transition(target, evidence=EVIDENCE)


@pytest.mark.ac("AC-7.1.3")
def test_completing_without_evidence__is_refused() -> None:
    with pytest.raises(MissingEvidenceError):
        task(_S.RUNNING).transition(_S.COMPLETED)


_REFERENCES = {
    EvidenceKind.RESOURCE: "order:3412987711",
    EvidenceKind.EXECUTION: "0190a000-0000-7000-8000-0000000000ee",
    EvidenceKind.APPROVAL: "0190a000-0000-7000-8000-0000000000ef",
}


@pytest.mark.ac("AC-7.1.3")
@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_completing__keeps_the_evidence_it_references(kind: EvidenceKind) -> None:
    evidence = TaskEvidence(kind, _REFERENCES[kind])

    done = task(_S.RUNNING).transition(_S.COMPLETED, evidence=evidence)

    assert done.evidence == evidence


@pytest.mark.ac("AC-7.1.3")
def test_evidence__is_a_resource_an_execution_or_an_approval_never_text() -> None:
    assert {k.value for k in EvidenceKind} == {"resource", "execution", "approval"}
    assert TaskEvidence(EvidenceKind.RESOURCE, "listing_variant:a-1B").reference


@pytest.mark.ac("AC-7.1.3")
@pytest.mark.parametrize(
    ("kind", "reference"),
    [
        (EvidenceKind.RESOURCE, " "),
        (EvidenceKind.RESOURCE, "order 3412987711"),  # spaces: free text
        (EvidenceKind.RESOURCE, "order:3412 987711"),
        (EvidenceKind.RESOURCE, "the buyer said it arrived"),
        (EvidenceKind.RESOURCE, "Order:1"),  # the kind is lower case
        (EvidenceKind.RESOURCE, "order:"),
        (EvidenceKind.RESOURCE, "order:" + "1" * 101),
        (EvidenceKind.RESOURCE, "order:1\n"),
        (EvidenceKind.EXECUTION, "exec-1"),  # not a UUID
        (EvidenceKind.EXECUTION, "0190A000-0000-7000-8000-0000000000EE"),  # not canonical
        (EvidenceKind.EXECUTION, "{0190a000-0000-7000-8000-0000000000ee}"),
        (EvidenceKind.APPROVAL, "order:1"),
        (EvidenceKind.APPROVAL, "0190a0000000700080000000000000ef"),
    ],
)
def test_evidence__a_reference_of_another_shape__is_refused(
    kind: EvidenceKind, reference: str
) -> None:
    """Sage-arch on #181: a strict shape, never free text."""
    with pytest.raises(ValueError, match="reference"):
        TaskEvidence(kind, reference)


def test_evidence_on_another_transition__is_refused() -> None:
    with pytest.raises(ValueError, match="evidence"):
        task(_S.CREATED).transition(_S.QUEUED, evidence=EVIDENCE)


@pytest.mark.ac("AC-7.1.4")
def test_one_task_model__with_the_kinds_of_aud_03() -> None:
    assert {"human_channel", "relay", "approval_follow_up"} <= {k.value for k in TaskKind}


@pytest.mark.parametrize(
    "changes",
    [
        {"idempotency_key": " "},
        {"idempotency_key": "k" * 201},
        {"correlation_id": ""},
        {"due_at": datetime(2026, 10, 27, 9, 0)},  # noqa: DTZ001 - naive on purpose
    ],
)
def test_invalid_task__is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each field has its own message
        task(_S.CREATED, **changes)


def test_due_at__is_kept_in_utc() -> None:
    due = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)

    assert task(due_at=due).due_at == due
