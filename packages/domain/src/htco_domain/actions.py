"""Business actions (ADR-0019, ADR-0022): approval policies and the action execution lifecycle.

Every business action of an automation or an employee runs through the Action Gateway, which
records one action execution per `(action, idempotency_key)` and moves it through an explicit
state machine (`docs/architecture/execution-engine.md` §7).
"""

from collections.abc import Mapping
from enum import StrEnum
from itertools import pairwise

from htco_domain.state_machine import check_transition


class ApprovalPolicy(StrEnum):
    R0 = "R0"  # read, no buyer data
    R1 = "R1"  # read with buyer or account data: masked by role, audited where personal
    W1 = "W1"  # write: a decided approval, the outbox, re-read verification, audited
    W2 = "W2"  # W1 plus step-up and a per-action flag
    W3 = "W3"  # W2, decided by the Owner only

    @property
    def is_write(self) -> bool:
        return self in {ApprovalPolicy.W1, ApprovalPolicy.W2, ApprovalPolicy.W3}

    @property
    def audited(self) -> bool:
        """Writes and reads of personal data leave an audit record."""
        return self is not ApprovalPolicy.R0


class VerifyMode(StrEnum):
    NONE = "none"
    REREAD = "reread"
    SCHEMA = "schema"
    HUMAN = "human"


class ActionState(StrEnum):
    RECEIVED = "received"
    AUTHORIZED = "authorized"
    WAITING_APPROVAL = "waiting_approval"
    ROUTED = "routed"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DEFERRED = "deferred"
    HANDED_TO_HUMAN = "handed_to_human"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


_S = ActionState
_ACTION_TRANSITIONS: Mapping[ActionState, frozenset[ActionState]] = {
    _S.RECEIVED: frozenset({_S.AUTHORIZED, _S.DENIED}),
    _S.AUTHORIZED: frozenset({_S.WAITING_APPROVAL, _S.ROUTED, _S.DENIED}),
    # Resubmitted: every check runs again, and may now deny.
    _S.WAITING_APPROVAL: frozenset({_S.AUTHORIZED, _S.REJECTED, _S.EXPIRED, _S.DENIED}),
    _S.ROUTED: frozenset({_S.RUNNING, _S.DEFERRED, _S.DENIED}),
    _S.RUNNING: frozenset({_S.VERIFYING, _S.SUCCEEDED, _S.DEFERRED, _S.HANDED_TO_HUMAN, _S.FAILED}),
    _S.VERIFYING: frozenset({_S.SUCCEEDED, _S.FAILED}),
    _S.DEFERRED: frozenset({_S.ROUTED}),
    _S.HANDED_TO_HUMAN: frozenset({_S.RUNNING, _S.CANCELLED}),
    # A retry with the same key runs every check again before it is routed.
    _S.FAILED: frozenset({_S.AUTHORIZED, _S.ROUTED, _S.DENIED, _S.DEAD_LETTERED}),
}
ACTION_FINAL_STATES = frozenset(
    {_S.SUCCEEDED, _S.DENIED, _S.REJECTED, _S.EXPIRED, _S.DEAD_LETTERED, _S.CANCELLED}
)


def check_action_transition(current: ActionState, target: ActionState) -> None:
    check_transition("action execution", _ACTION_TRANSITIONS, current, target)


def action_path(*states: ActionState) -> None:
    """Check a whole path, for example RECEIVED → AUTHORIZED → ROUTED → RUNNING → SUCCEEDED."""
    for current, target in pairwise(states):
        check_action_transition(current, target)
