"""The shared approval contract (slice 5.14, ADR-0019, `execution-engine.md` §5).

One contract for users, automations, employees and browser actions. Phase 5 ships the
`ApprovalService` port, the `ApprovalRequest` and `Approval` values and `MemoryApprovalService`;
the Postgres store, the inbox UI and notifications are Phase 7.

- **The request hash** is the gateway's `request_hash`: SHA-256 of everything that will be sent
  (action and version, actor, on-behalf-of user, organization, shop, idempotency key, input). An
  approval covers exactly that request: a resubmission whose hash differs is refused
  (AC-5.14.1).
- **Deciding** (`decide`): a person only (an `AuthContext`), never the requester; the policy's
  roles (W3: the Owner); a second factor, and for W2 and W3 a recent re-authentication; the hash
  the person was shown must be the request's; a lapsed approval is `EXPIRED`.
- **At most once** (AC-5.14.2): the gateway stores one action execution per request, and tells
  the service when the approved request succeeded (`executed`): the approval is then `EXECUTED`
  and bound to that execution; it never covers another one.
- **Waiting automations** (AC-5.14.3): an automation's action that needs an approval returns
  `PENDING_APPROVAL`; its handler returns `Waiting`, so the execution is `WAITING` and holds no
  job. By convention the handler passes its own execution ID as the request's `causation_id`;
  the approval keeps it, and `ApprovalResumer` resumes that execution once when the approval is
  approved, or cancels it with the reason when it is rejected or expires.
- `GatewayApprovals` adapts the service to the gateway's `ApprovalGate` port.
"""

import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import STEP_UP_WINDOW
from htco_application.automation.durable import ExecutionNotFoundError, NotWaitingError
from htco_application.execution.contracts import ActionDefinition, Evidence
from htco_application.execution.gateway import ActionRef, ActionRequest
from htco_application.kernel import JsonValue
from htco_domain.actions import ApprovalPolicy
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.approvals import (
    APPROVAL_FINAL_STATES,
    ApprovalState,
    Decision,
    check_approval_transition,
    decider_roles,
    needs_step_up,
)
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.tooling import RiskLevel

DEFAULT_TTL = timedelta(days=3)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_A = ApprovalState


@dataclass(frozen=True)
class ApprovalRequest:
    action: ActionRef
    request_hash: str  # SHA-256 of the canonical ActionRequest, without approval_id
    requester: ActorRef
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    summary: str  # a code template, never model text
    parameters: Mapping[str, JsonValue]  # what will be sent
    impact: str
    risk: RiskLevel
    rollback: str
    expires_at: datetime
    policy: ApprovalPolicy
    decided_by_roles: frozenset[Role]
    requires_step_up: bool
    evidence: tuple[Evidence, ...] = ()
    # The automation execution that waits for the decision (the request's causation_id).
    waiting_execution_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not _SHA256.match(self.request_hash):
            raise ValueError("an approval covers a SHA-256 request hash")
        if self.decided_by_roles != decider_roles(self.policy):  # raises for a read policy
            raise ValueError(f"{self.policy.value} is decided by its policy's roles only")
        if self.requires_step_up != needs_step_up(self.policy):
            raise ValueError(f"step-up is {needs_step_up(self.policy)} for {self.policy.value}")
        if self.expires_at.tzinfo is None:
            raise ValueError("an approval expires at a time in UTC")
        if not self.summary.strip():
            raise ValueError("an approval needs a summary")


@dataclass(frozen=True)
class Approval:
    id: uuid.UUID
    request: ApprovalRequest
    state: ApprovalState
    requested_at: datetime
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    shown_hash: str | None = None
    execution_id: uuid.UUID | None = None  # the action execution it ran as

    def transition(self, target: ApprovalState, **changes: object) -> Approval:
        check_approval_transition(self.state, target)
        return replace(self, state=target, **changes)  # type: ignore[arg-type]

    @property
    def is_final(self) -> bool:
        return self.state in APPROVAL_FINAL_STATES


class ApprovalNotFoundError(Exception):
    pass


class DecisionRefusedError(Exception):
    """A decision that the rules forbid; `code` is stable for the API (Phase 7)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


class AlreadyExecutedError(Exception):
    """The approval ran already, as another action execution."""


class ApprovalService(Protocol):  # contract in Phase 5, real in Phase 7
    def request(self, request: ApprovalRequest) -> Approval: ...

    def decide(
        self, approval_id: uuid.UUID, decision: Decision, *, by: AuthContext, shown_hash: str
    ) -> Approval: ...

    def status(self, approval_id: uuid.UUID) -> Approval: ...

    def executed(self, approval_id: uuid.UUID, *, execution_id: uuid.UUID) -> Approval: ...


def check_decider(approval: Approval, by: AuthContext, *, shown_hash: str, now: datetime) -> None:
    """The rules every decision passes; raises `DecisionRefusedError`."""
    request = approval.request
    if request.requester == ActorRef(ActorKind.USER, str(by.user_id)):
        raise DecisionRefusedError("own_request", "the requester never decides its approval")
    try:
        principal = by.principal_for(request.organization_id)
    except NoMembershipError:
        raise DecisionRefusedError("not_found", "no such approval for the user") from None
    if principal.role not in request.decided_by_roles:
        raise DecisionRefusedError("forbidden", f"{principal.role.value} may not decide this")
    if principal.assurance is not AssuranceLevel.AAL2:
        raise DecisionRefusedError("mfa_required", "a decision needs a second factor")
    at = principal.reauthenticated_at
    if request.requires_step_up and not (at is not None and now - STEP_UP_WINDOW < at <= now):
        raise DecisionRefusedError("step_up_required", "this decision needs a re-authentication")
    if shown_hash != request.request_hash:
        raise DecisionRefusedError("hash_mismatch", "the decision was shown another request")


@dataclass
class MemoryApprovalService:
    """The contract in memory, for the gateway's and the engine's tests (Postgres: Phase 7)."""

    clock: Callable[[], datetime]
    approvals: dict[uuid.UUID, Approval] = field(default_factory=dict)
    # Called once per decision or expiry, after it is stored (the resumer, notifications).
    listeners: list[Callable[[Approval], None]] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def request(self, request: ApprovalRequest) -> Approval:
        """One open approval per request: asking again returns it."""
        with self._lock:
            for approval in self.approvals.values():
                if (
                    approval.request.organization_id == request.organization_id
                    and approval.request.request_hash == request.request_hash
                    and approval.state in {_A.REQUESTED, _A.APPROVED}
                ):
                    return self._lapse(approval)
            approval = Approval(uuid.uuid7(), request, _A.REQUESTED, requested_at=self.clock())
            self.approvals[approval.id] = approval
            return approval

    def decide(
        self, approval_id: uuid.UUID, decision: Decision, *, by: AuthContext, shown_hash: str
    ) -> Approval:
        with self._lock:
            approval = self.status(approval_id)
            if approval.state is not _A.REQUESTED:
                raise DecisionRefusedError("not_pending", f"the approval is {approval.state}")
            now = self.clock()
            check_decider(approval, by, shown_hash=shown_hash, now=now)
            target = _A.APPROVED if decision is Decision.APPROVE else _A.REJECTED
            decided = approval.transition(
                target, decided_by=by.user_id, decided_at=now, shown_hash=shown_hash
            )
            self.approvals[approval_id] = decided
        self._tell(decided)
        return decided

    def status(self, approval_id: uuid.UUID) -> Approval:
        with self._lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFoundError(str(approval_id))
            return self._lapse(approval)

    def executed(self, approval_id: uuid.UUID, *, execution_id: uuid.UUID) -> Approval:
        with self._lock:
            approval = self.status(approval_id)
            if approval.state is _A.EXECUTED:
                if approval.execution_id != execution_id:
                    raise AlreadyExecutedError(f"{approval_id} ran as {approval.execution_id}")
                return approval
            done = approval.transition(_A.EXECUTED, execution_id=execution_id)
            self.approvals[approval_id] = done
            return done

    def _lapse(self, approval: Approval) -> Approval:
        """An open approval past its time is EXPIRED (checked whenever it is read)."""
        if approval.is_final or self.clock() < approval.request.expires_at:
            return approval
        expired = approval.transition(_A.EXPIRED)
        self.approvals[approval.id] = expired
        self._tell(expired)
        return expired

    def _tell(self, approval: Approval) -> None:
        for listener in self.listeners:
            listener(approval)


def approval_request_for(
    request: ActionRequest,
    *,
    request_hash: str,
    definition: ActionDefinition,
    now: datetime,
    ttl: timedelta = DEFAULT_TTL,
) -> ApprovalRequest:
    """The approval for a gateway request; every text is a template, never model output."""
    policy = definition.approval_policy
    waiting = None
    if request.actor.kind is ActorKind.AUTOMATION and request.causation_id is not None:
        try:
            waiting = uuid.UUID(request.causation_id)
        except ValueError:
            waiting = None
    tool = definition.tool
    return ApprovalRequest(
        action=request.action,
        request_hash=request_hash,
        requester=request.actor,
        organization_id=request.organization_id,
        shop_id=request.shop_id,
        summary=f"Run {definition.name} v{definition.version}",
        parameters=dict(request.input),
        impact=f"{tool.side_effect.value} write, {tool.data_scope.value} data",
        risk=tool.risk,
        rollback=f"See the runbook of {definition.name}",
        expires_at=now + ttl,
        policy=policy,
        decided_by_roles=decider_roles(policy),
        requires_step_up=needs_step_up(policy),
        waiting_execution_id=waiting,
    )


class GatewayApprovals:
    """The gateway's `ApprovalGate` on an `ApprovalService`."""

    def __init__(self, service: ApprovalService, clock: Callable[[], datetime]) -> None:
        self._service = service
        self._clock = clock

    def request(
        self, request: ActionRequest, *, request_hash: str, definition: ActionDefinition
    ) -> uuid.UUID:
        return self._service.request(
            approval_request_for(
                request, request_hash=request_hash, definition=definition, now=self._clock()
            )
        ).id

    def is_approved(self, approval_id: uuid.UUID, *, request_hash: str) -> bool:
        try:
            approval = self._service.status(approval_id)
        except ApprovalNotFoundError:
            return False
        if approval.request.request_hash != request_hash:
            return False  # AC-5.14.1: it covers exactly the request that was shown
        return approval.state is _A.APPROVED

    def executed(self, approval_id: uuid.UUID, *, execution_id: uuid.UUID) -> None:
        self._service.executed(approval_id, execution_id=execution_id)


class WaitingExecutions(Protocol):
    """What the resumer needs of the durable engine."""

    def resume(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, *, actor: ActorRef, reason: str
    ) -> object: ...

    def cancel_waiting(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, *, actor: ActorRef, reason: str
    ) -> object: ...


_ENDINGS = {_A.REJECTED: "approval_rejected", _A.EXPIRED: "approval_expired"}


class ApprovalResumer:
    """On a decision, the waiting automation execution resumes once or ends CANCELLED."""

    def __init__(self, engine: WaitingExecutions) -> None:
        self._engine = engine

    def __call__(self, approval: Approval) -> None:
        execution_id = approval.request.waiting_execution_id
        if execution_id is None:
            return
        organization = approval.request.organization_id
        actor = (
            ActorRef(ActorKind.USER, str(approval.decided_by))
            if approval.decided_by is not None
            else ActorRef(ActorKind.SYSTEM, "approvals")
        )
        try:
            if approval.state is _A.APPROVED:
                self._engine.resume(
                    organization, execution_id, actor=actor, reason="approval_approved"
                )
            elif approval.state in _ENDINGS:
                self._engine.cancel_waiting(
                    organization, execution_id, actor=actor, reason=_ENDINGS[approval.state]
                )
        except NotWaitingError, ExecutionNotFoundError:
            return  # resumed or ended already: a decision moves a waiting execution once
