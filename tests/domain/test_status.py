"""One status vocabulary (slice 5.18): every state of every lifecycle has one kind."""

from enum import Enum

import pytest

from htco_domain.actions import ACTION_FINAL_STATES, ActionState
from htco_domain.automation import ExecutionState
from htco_domain.browser import EnvironmentStatus
from htco_domain.status import StatusKind, WaitingReason, status_kind, vocabularies, waiting_reason
from htco_domain.tasks import TaskState

LIFECYCLES: list[type[Enum]] = [ExecutionState, ActionState, EnvironmentStatus, TaskState]


@pytest.mark.parametrize("lifecycle", LIFECYCLES)
def test_every_state__has_exactly_one_kind(lifecycle: type[Enum]) -> None:
    assert set(vocabularies()[lifecycle]) == set(lifecycle)
    assert all(isinstance(status_kind(state), StatusKind) for state in lifecycle)  # type: ignore[arg-type]


def test_vocabulary__covers_the_four_lifecycles() -> None:
    assert set(vocabularies()) == set(LIFECYCLES)


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
