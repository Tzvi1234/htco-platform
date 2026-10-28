"""One status vocabulary across the CRM (slice 5.18, `execution-engine.md` §7).

Every lifecycle keeps its own state machine: automation executions (`ExecutionState`), action
executions (`ActionState`), browser environments (`EnvironmentStatus`), business tasks
(`TaskState`), incidents (`IncidentState`, Phase 6), jobs (`JobState`), approvals
(`ApprovalState`) and orders (`OrderState`, AUD-03). For the office, each state also has one
`StatusKind`, so a screen or a monitor can say "running", "waiting", "done", "failed" or
"stopped" the same way for all of them. A task or an execution that waits says why with a
`WaitingReason`; the engine records these on the task rather than as new task states.
"""

from collections.abc import Mapping
from enum import Enum, StrEnum

from htco_domain.actions import ActionState
from htco_domain.approvals import ApprovalState
from htco_domain.automation import ExecutionState
from htco_domain.browser import EnvironmentStatus
from htco_domain.incidents import IncidentState
from htco_domain.jobs import JobState
from htco_domain.orders import OrderState
from htco_domain.tasks import TaskState


class StatusKind(StrEnum):
    ACTIVE = "active"  # queued, running, ready to work
    WAITING = "waiting"  # on a person, an approval, an outside system or a retry
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # failed, dead-lettered or unhealthy
    STOPPED = "stopped"  # cancelled, denied, rejected, expired, suspended or not configured


class WaitingReason(StrEnum):
    WAITING_FOR_HUMAN = "waiting_for_human"  # the Human channel: a login, a challenge, a refund
    WAITING_FOR_APPROVAL = "waiting_for_approval"


_K = StatusKind
_X = ExecutionState
_A = ActionState
_V = EnvironmentStatus
_T = TaskState
_I = IncidentState
_J = JobState
_P = ApprovalState
_O = OrderState

_KINDS: Mapping[type[Enum], Mapping[Enum, StatusKind]] = {
    ExecutionState: {
        _X.PENDING: _K.ACTIVE, _X.RUNNING: _K.ACTIVE, _X.WAITING: _K.WAITING,
        _X.SUCCEEDED: _K.SUCCEEDED, _X.FAILED: _K.FAILED, _X.RETRY_SCHEDULED: _K.WAITING,
        _X.DEAD_LETTERED: _K.FAILED, _X.COMPENSATED: _K.STOPPED, _X.CANCELLED: _K.STOPPED,
    },
    ActionState: {
        _A.RECEIVED: _K.ACTIVE, _A.AUTHORIZED: _K.ACTIVE, _A.WAITING_APPROVAL: _K.WAITING,
        _A.ROUTED: _K.ACTIVE, _A.RUNNING: _K.ACTIVE, _A.VERIFYING: _K.ACTIVE,
        _A.SUCCEEDED: _K.SUCCEEDED, _A.DENIED: _K.STOPPED, _A.REJECTED: _K.STOPPED,
        _A.EXPIRED: _K.STOPPED, _A.DEFERRED: _K.WAITING, _A.HANDED_TO_HUMAN: _K.WAITING,
        _A.FAILED: _K.FAILED, _A.DEAD_LETTERED: _K.FAILED, _A.CANCELLED: _K.STOPPED,
    },
    EnvironmentStatus: {
        _V.NOT_CONFIGURED: _K.STOPPED, _V.PROVISIONING: _K.ACTIVE, _V.READY: _K.ACTIVE,
        _V.LOGIN_REQUIRED: _K.WAITING, _V.DEGRADED: _K.FAILED, _V.SUSPENDED: _K.STOPPED,
        _V.FAILED: _K.FAILED,
    },
    TaskState: {
        _T.CREATED: _K.ACTIVE, _T.QUEUED: _K.ACTIVE, _T.CLAIMED: _K.ACTIVE, _T.RUNNING: _K.ACTIVE,
        _T.WAITING_FOR_EXTERNAL: _K.WAITING, _T.WAITING_FOR_AGENT: _K.WAITING,
        _T.WAITING_FOR_APPROVAL: _K.WAITING, _T.RETRY_SCHEDULED: _K.WAITING,
        _T.BLOCKED: _K.WAITING, _T.COMPLETED: _K.SUCCEEDED, _T.FAILED: _K.FAILED,
        _T.ESCALATED: _K.WAITING, _T.CANCELLED: _K.STOPPED,
    },
    # Incidents (slice 6.3): open work is active; waiting on an approval, a developer or the
    # evidence is waiting; resolved on evidence is done.
    IncidentState: {
        _I.DETECTED: _K.ACTIVE, _I.TRIAGED: _K.ACTIVE, _I.INVESTIGATING: _K.ACTIVE,
        _I.REMEDIATION_PROPOSED: _K.ACTIVE, _I.AUTO_REMEDIATING: _K.ACTIVE,
        _I.APPROVAL_REQUIRED: _K.WAITING, _I.DEVELOPER_REQUIRED: _K.WAITING,
        _I.VERIFYING: _K.WAITING, _I.ESCALATED: _K.ACTIVE, _I.RESOLVED: _K.SUCCEEDED,
    },
    # Jobs: a retry waits for its time; dead is the dead-letter state.
    JobState: {
        _J.QUEUED: _K.ACTIVE, _J.RUNNING: _K.ACTIVE, _J.RETRY_SCHEDULED: _K.WAITING,
        _J.SUCCEEDED: _K.SUCCEEDED, _J.DEAD: _K.FAILED,
    },
    # Approvals: a request waits for a person; approved is ready to run once.
    ApprovalState: {
        _P.REQUESTED: _K.WAITING, _P.APPROVED: _K.ACTIVE, _P.REJECTED: _K.STOPPED,
        _P.EXPIRED: _K.STOPPED, _P.EXECUTED: _K.SUCCEEDED, _P.FAILED: _K.FAILED,
        _P.CANCELLED: _K.STOPPED,
    },
    # Orders: unpaid waits for the buyer; the fulfillment steps are active until delivered.
    OrderState: {
        _O.UNPAID: _K.WAITING, _O.PAID: _K.ACTIVE, _O.INGESTED: _K.ACTIVE,
        _O.AWAITING_SHIPMENT: _K.ACTIVE, _O.SHIPPED: _K.ACTIVE, _O.DELIVERED: _K.SUCCEEDED,
        _O.CANCELLED: _K.STOPPED, _O.REFUNDED: _K.STOPPED,
    },
}  # fmt: skip

_WAITING_REASONS: Mapping[Enum, WaitingReason] = {
    _A.WAITING_APPROVAL: WaitingReason.WAITING_FOR_APPROVAL,
    _A.HANDED_TO_HUMAN: WaitingReason.WAITING_FOR_HUMAN,
    _T.WAITING_FOR_APPROVAL: WaitingReason.WAITING_FOR_APPROVAL,
    _V.LOGIN_REQUIRED: WaitingReason.WAITING_FOR_HUMAN,
}


def status_kind(
    state: ExecutionState
    | ActionState
    | EnvironmentStatus
    | TaskState
    | IncidentState
    | JobState
    | ApprovalState
    | OrderState,
) -> StatusKind:
    return _KINDS[type(state)][state]


def waiting_reason(
    state: ExecutionState
    | ActionState
    | EnvironmentStatus
    | TaskState
    | IncidentState
    | JobState
    | ApprovalState
    | OrderState,
) -> WaitingReason | None:
    """Why a waiting state waits, when the state itself says it."""
    return _WAITING_REASONS.get(state)


def vocabularies() -> Mapping[type[Enum], Mapping[Enum, StatusKind]]:
    return _KINDS
