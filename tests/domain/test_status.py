"""One status vocabulary (slice 5.18): every state of every lifecycle has one kind."""

from enum import Enum

import pytest

from htco_domain.actions import ACTION_FINAL_STATES, ActionState
from htco_domain.approvals import APPROVAL_FINAL_STATES, ApprovalState
from htco_domain.automation import ExecutionState
from htco_domain.browser import EnvironmentStatus
from htco_domain.incidents import IncidentState
from htco_domain.jobs import JobState
from htco_domain.orders import OrderState
from htco_domain.status import StatusKind, WaitingReason, status_kind, vocabularies, waiting_reason
from htco_domain.tasks import TaskState

LIFECYCLES: list[type[Enum]] = [
    ExecutionState, ActionState, EnvironmentStatus, TaskState, IncidentState, JobState,
    ApprovalState, OrderState,
]  # fmt: skip


@pytest.mark.parametrize("lifecycle", LIFECYCLES)
def test_every_state__has_exactly_one_kind(lifecycle: type[Enum]) -> None:
    assert set(vocabularies()[lifecycle]) == set(lifecycle)
    assert all(isinstance(status_kind(state), StatusKind) for state in lifecycle)  # type: ignore[arg-type]


@pytest.mark.ac("AUD-03.4")
def test_vocabulary__covers_every_lifecycle() -> None:
    assert set(vocabularies()) == set(LIFECYCLES)


@pytest.mark.ac("AUD-03.4")
@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (JobState.DEAD, StatusKind.FAILED),
        (JobState.RETRY_SCHEDULED, StatusKind.WAITING),
        (ApprovalState.REQUESTED, StatusKind.WAITING),
        (ApprovalState.EXECUTED, StatusKind.SUCCEEDED),
        (OrderState.UNPAID, StatusKind.WAITING),
        (OrderState.DELIVERED, StatusKind.SUCCEEDED),
        (OrderState.REFUNDED, StatusKind.STOPPED),
    ],
)
def test_jobs_approvals_and_orders__have_their_kind(state: Enum, kind: StatusKind) -> None:
    assert status_kind(state) is kind  # type: ignore[arg-type]


def test_final_approval_states__are_never_active_or_waiting() -> None:
    assert {status_kind(s) for s in APPROVAL_FINAL_STATES} <= {
        StatusKind.SUCCEEDED,
        StatusKind.FAILED,
        StatusKind.STOPPED,
    }


def test_final_action_states__are_never_active_or_waiting() -> None:
    assert {status_kind(s) for s in ACTION_FINAL_STATES} <= {
        StatusKind.SUCCEEDED,
        StatusKind.FAILED,
        StatusKind.STOPPED,
    }


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (ActionState.WAITING_APPROVAL, WaitingReason.WAITING_FOR_APPROVAL),
        (ActionState.HANDED_TO_HUMAN, WaitingReason.WAITING_FOR_HUMAN),
        (TaskState.WAITING_FOR_APPROVAL, WaitingReason.WAITING_FOR_APPROVAL),
        (EnvironmentStatus.LOGIN_REQUIRED, WaitingReason.WAITING_FOR_HUMAN),
        (ExecutionState.RUNNING, None),
    ],
)
def test_waiting_reason__said_by_the_state(state: object, reason: WaitingReason | None) -> None:
    assert waiting_reason(state) is reason  # type: ignore[arg-type]


def test_waiting_reasons__wait() -> None:
    for lifecycle in LIFECYCLES:
        for state in lifecycle:
            if waiting_reason(state) is not None:  # type: ignore[arg-type]
                assert status_kind(state) is StatusKind.WAITING  # type: ignore[arg-type]
