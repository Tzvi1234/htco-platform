"""The approval lifecycle and its deciders (slice 5.14, `execution-engine.md` §5)."""

from itertools import product

import pytest

from htco_domain.actions import ApprovalPolicy
from htco_domain.approvals import (
    APPROVAL_FINAL_STATES,
    ApprovalState,
    check_approval_transition,
    decider_roles,
    needs_step_up,
)
from htco_domain.identity import Role
from htco_domain.state_machine import IllegalTransition

_A = ApprovalState
ALLOWED = {
    (_A.REQUESTED, _A.APPROVED),
    (_A.REQUESTED, _A.REJECTED),
    (_A.REQUESTED, _A.EXPIRED),
    (_A.REQUESTED, _A.CANCELLED),
    (_A.APPROVED, _A.EXECUTED),
    (_A.APPROVED, _A.FAILED),
    (_A.APPROVED, _A.EXPIRED),
    (_A.APPROVED, _A.CANCELLED),
}


def test_approval_state__has_the_seven_states() -> None:
    assert [s.value for s in _A] == [
        "requested", "approved", "rejected", "expired", "executed", "failed", "cancelled",
    ]  # fmt: skip


@pytest.mark.parametrize(("current", "target"), sorted(ALLOWED))
def test_transition__allowed(current: ApprovalState, target: ApprovalState) -> None:
    check_approval_transition(current, target)


@pytest.mark.ac("AC-5.14.2")
@pytest.mark.parametrize(("current", "target"), [p for p in product(_A, _A) if p not in ALLOWED])
def test_transition__anything_else__refused(current: ApprovalState, target: ApprovalState) -> None:
    """An executed approval never runs again; a decided one is never decided again."""
    with pytest.raises(IllegalTransition):
        check_approval_transition(current, target)


def test_final_states__have_no_way_out() -> None:
    assert {current for current, _ in ALLOWED}.isdisjoint(APPROVAL_FINAL_STATES)


def test_deciders__w3_is_the_owners_alone() -> None:
    assert decider_roles(ApprovalPolicy.W3) == {Role.OWNER}
    assert (
        decider_roles(ApprovalPolicy.W1)
        == decider_roles(ApprovalPolicy.W2)
        == {
            Role.OWNER,
            Role.ADMIN,
        }
    )


@pytest.mark.parametrize("policy", [ApprovalPolicy.R0, ApprovalPolicy.R1])
def test_deciders__a_read_needs_no_approval(policy: ApprovalPolicy) -> None:
    with pytest.raises(ValueError, match="read"):
        decider_roles(policy)


def test_step_up__for_w2_and_w3() -> None:
    assert [p for p in ApprovalPolicy if needs_step_up(p)] == [ApprovalPolicy.W2, ApprovalPolicy.W3]
