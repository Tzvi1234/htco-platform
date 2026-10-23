"""Approvals (ADR-0019, `execution-engine.md` §5, slice 5.14): one contract for every actor and
channel. Phase 5 defines the lifecycle and the decision rules; Phase 7 stores them in Postgres.

Rules (AC-7.3.x, carried from the design):

- only a person decides, and never the requester of the approval;
- W1 and W2 are decided by the Owner or an Admin, W3 by the Owner only;
- every decision needs a second factor; W2 and W3 also need a recent re-authentication;
- the decision stores the hash that was shown, which must be the request's;
- an approval executes at most once.
"""

from collections.abc import Mapping
from enum import StrEnum

from htco_domain.actions import ApprovalPolicy
from htco_domain.identity import Role
from htco_domain.state_machine import check_transition


class ApprovalState(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


_A = ApprovalState
_APPROVAL_TRANSITIONS: Mapping[ApprovalState, frozenset[ApprovalState]] = {
    _A.REQUESTED: frozenset({_A.APPROVED, _A.REJECTED, _A.EXPIRED, _A.CANCELLED}),
    # Approved but not yet run: it runs once (EXECUTED or FAILED), or lapses.
    _A.APPROVED: frozenset({_A.EXECUTED, _A.FAILED, _A.EXPIRED, _A.CANCELLED}),
}
APPROVAL_FINAL_STATES = frozenset({_A.REJECTED, _A.EXPIRED, _A.EXECUTED, _A.FAILED, _A.CANCELLED})


def check_approval_transition(current: ApprovalState, target: ApprovalState) -> None:
    check_transition("approval", _APPROVAL_TRANSITIONS, current, target)


def decider_roles(policy: ApprovalPolicy) -> frozenset[Role]:
    """Who may decide an approval of this policy (PERMISSION_MATRIX §1a)."""
    if not policy.is_write:
        raise ValueError(f"{policy.value} is a read: it needs no approval")
    if policy is ApprovalPolicy.W3:
        return frozenset({Role.OWNER})
    return frozenset({Role.OWNER, Role.ADMIN})


def needs_step_up(policy: ApprovalPolicy) -> bool:
    return policy in {ApprovalPolicy.W2, ApprovalPolicy.W3}
