"""The Action Gateway (slice 5.12, ADR-0019, `execution-engine.md` §4).

`submit` is the only entry point for business actions:

1. **Claim** `(action, idempotency_key)`: a finished action returns its stored result (a replay
   runs nothing, AC-5.12.1); one still in progress answers `DEFERRED`.
2. **Validate** the action, its version and its input against the action's JSON Schema.
3. **Authorize** the actor: users through the policy engine and the action's `user_capability`
   (a shop the user may not use does not exist: `not_found`), employees through
   `decide_tool_access`, automations through their specification's `allowed_actions`, the
   platform (`system`) for R0 reads only, advisors never (AC-5.12.2, AC-5.12.5).
4. **Approve**: a write (W1 to W3) runs only with a decided approval for exactly this request, or
   the Owner's own trigger; otherwise an approval is requested and the result is
   `PENDING_APPROVAL`, running nothing (AC-5.12.3). Resubmitted with the approval, every check runs
   again.
5. **Route** to the first binding whose channel is deployed and whose shop capability is there
   (the full Execution Router is slice 5.13; limits and breakers are 5.15).
6. **Execute** through the channel adapter, then **verify** (re-read, schema or none).
7. **Record**: the action execution's final state and result, an audit record for writes and
   personal data, and `actions.executed.v1` through the outbox, in one transaction (AC-5.12.4).

Every refusal is a result with a stable `error_code` and a reason that never contains the input
(it may be personal) or a secret.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from htco_application.access.policy import Outcome, Principal, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.execution.contracts import (
    ActionCatalog,
    ActionDefinition,
    ChannelBinding,
    ChannelOutcome,
)
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    Channel,
)
from htco_application.execution.schema import validate
from htco_application.jobs import JsonValue, describe_error
from htco_application.messaging import OutboxMessage
from htco_application.workforce.registry import Registry
from htco_application.workforce.tool_access import ToolVerdict, decide_tool_access
from htco_domain.actions import ActionState, ApprovalPolicy, VerifyMode, action_path
from htco_domain.actors import ActorKind
from htco_domain.identity import Role

EVENT_DESTINATION = "events.publish"
EXECUTED_EVENT = "actions.executed.v1"

_S = ActionState
# A claimed action that a resubmission continues: every check runs again.
_RESUMABLE = frozenset({_S.WAITING_APPROVAL, _S.FAILED})


def request_hash(request: ActionRequest) -> str:
    """SHA-256 of everything that will be sent, without the approval itself (§5)."""
    canonical = {
        "action": [request.action.name, request.action.version],
        "actor": str(request.actor),
        "on_behalf_of_user": None
        if request.on_behalf_of_user is None
        else str(request.on_behalf_of_user),
        "organization_id": str(request.organization_id),
        "shop_id": None if request.shop_id is None else str(request.shop_id),
        "idempotency_key": request.idempotency_key,
        "input": request.input,
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Claim:
    execution_id: uuid.UUID
    created: bool
    state: ActionState
    result: ActionResult | None  # the stored result of a finished action
    approval_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Finish:
    """What one recorded step writes."""

    state: ActionState
    path: tuple[ActionState, ...]  # the states passed since the last record, checked in order
    result: ActionResult
    reason: str | None = None
    approval_id: uuid.UUID | None = None
    audit: AuditRecord | None = None
    event: OutboxMessage | None = None
    # An R1 read's output may hold buyer data: it is returned, never stored.
    store_output: bool = True


class ActionStore(Protocol):
    def claim(self, request: ActionRequest, *, request_hash: str, now: datetime) -> Claim:
        """Insert `(action, idempotency_key)` as RECEIVED unless it exists; return it."""
        ...

    def record(self, execution_id: uuid.UUID, step: Finish, *, now: datetime) -> None:
        """Store the step, its audit record and its event in one transaction."""
        ...


class Principals(Protocol):
    def principal(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> Principal | None: ...


class ShopOwners(Protocol):
    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None: ...


class ApprovalGate(Protocol):
    """The shared approval contract (slice 5.14; Postgres in Phase 7)."""

    def request(
        self, request: ActionRequest, *, request_hash: str, definition: ActionDefinition
    ) -> uuid.UUID: ...

    def is_approved(self, approval_id: uuid.UUID, *, request_hash: str) -> bool: ...


class ChannelAdapter(Protocol):
    channel: Channel

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome: ...

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool: ...


class ShopCapabilities(Protocol):
    def available(self, shop_id: uuid.UUID, capability: str) -> bool: ...


@dataclass(frozen=True)
class GatewayParts:
    catalog: ActionCatalog
    registry: Callable[[], Registry]  # employees and automations (their grants)
    store: ActionStore
    principals: Principals
    shops: ShopOwners
    approvals: ApprovalGate
    adapters: Mapping[Channel, ChannelAdapter]
    capabilities: ShopCapabilities
    clock: Callable[[], datetime]


class Refusal(Exception):  # noqa: N818 - a refusal is the result, not a failure of the gateway
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class ActionGatewayService:
    def __init__(self, parts: GatewayParts) -> None:
        self._p = parts

    async def submit(self, request: ActionRequest) -> ActionResult:
        now = self._p.clock()
        digest = request_hash(request)
        claim = self._p.store.claim(request, request_hash=digest, now=now)
        if claim.result is not None:
            return claim.result
        if not claim.created and claim.state not in _RESUMABLE:
            return self._result(claim.execution_id, ActionOutcome.DEFERRED, error="in_progress")
        path: list[ActionState] = [claim.state]
        try:
            definition = self._definition(request)
            forced_approval = self._authorize(request, definition)
            path.append(_S.AUTHORIZED)
            if (definition.approval_policy.is_write or forced_approval) and not (
                self._approved(request, digest, definition)
            ):
                return self._wait_for_approval(claim, request, definition, digest, path, now)
        except Refusal as refusal:
            return self._deny(claim, request, refusal, path, now)
        return await self._run(claim, request, definition, path)

    # --- checks -----------------------------------------------------------------------------

    def _definition(self, request: ActionRequest) -> ActionDefinition:
        definition = self._p.catalog.get(request.action.name, request.action.version)
        if definition is None:
            raise Refusal(
                "unknown_action", f"no action {request.action.name} v{request.action.version}"
            )
        problems = validate(dict(request.input), definition.input_schema)
        if problems:
            raise Refusal("invalid_input", "; ".join(problems[:5]))
        return definition

    def _authorize(self, request: ActionRequest, definition: ActionDefinition) -> bool:
        """Raises `Refusal`; True when this actor needs an approval even for a read."""
        kind = request.actor.kind
        if kind is ActorKind.ADVISOR:
            raise Refusal("forbidden", "advisors never act")
        if kind is ActorKind.SYSTEM:
            if definition.approval_policy is not ApprovalPolicy.R0:
                raise Refusal("forbidden", "the platform runs R0 reads only")
            return False
        if kind is ActorKind.AUTOMATION:
            automation = self._p.registry().automations.get(request.actor.id)
            if automation is None or definition.name not in automation.allowed_actions:
                raise Refusal(
                    "forbidden", f"{definition.name} is outside {request.actor.id}'s permissions"
                )
            return False
        if kind is ActorKind.EMPLOYEE:
            access = decide_tool_access(self._p.registry(), request.actor.id, definition.name)
            if access.verdict is ToolVerdict.DENY:
                raise Refusal("forbidden", access.reason)
            return access.verdict is ToolVerdict.REQUIRE_APPROVAL
        self._authorize_user(request, definition)
        return False

    def _authorize_user(self, request: ActionRequest, definition: ActionDefinition) -> None:
        principal = self._p.principals.principal(
            uuid.UUID(request.actor.id), request.organization_id
        )
        if principal is None:
            raise Refusal("not_found", "no such organization for the user")
        if definition.user_capability is None:
            raise Refusal("forbidden", f"no user may start {definition.name}")
        if (
            decide(principal, definition.user_capability, shop=None, now=self._p.clock()).outcome
            is not Outcome.ALLOW
        ):
            raise Refusal("forbidden", f"the role may not use {definition.name}")
        if request.shop_id is not None:
            owner = self._p.shops.organization_of_shop(request.shop_id)
            shop = None if owner is None else ShopRef(id=request.shop_id, organization_id=owner)
            if (
                shop is None
                or decide(
                    principal, definition.user_capability, shop=shop, now=self._p.clock()
                ).outcome
                is not Outcome.ALLOW
            ):
                raise Refusal("not_found", "no such shop for the user")
        if request.owner_trigger and principal.role is not Role.OWNER:
            raise Refusal("forbidden", "only the Owner triggers a write by hand")

    def _approved(
        self,
        request: ActionRequest,
        digest: str,
        definition: ActionDefinition,  # noqa: ARG002 - W3's Owner-only rule arrives with 5.14
    ) -> bool:
        if request.owner_trigger and request.actor.kind is ActorKind.USER:
            return True  # `_authorize_user` checked that the Owner triggered it
        if request.approval_id is None:
            return False
        if not self._p.approvals.is_approved(request.approval_id, request_hash=digest):
            raise Refusal("approval_invalid", "the approval does not cover this request")
        return True

    # --- outcomes ---------------------------------------------------------------------------

    def _wait_for_approval(  # noqa: PLR0913, PLR0917 - the claim, the request and its checks
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        digest: str,
        path: list[ActionState],
        now: datetime,
    ) -> ActionResult:
        approval_id = claim.approval_id or self._p.approvals.request(
            request, request_hash=digest, definition=definition
        )
        result = self._result(claim.execution_id, ActionOutcome.PENDING_APPROVAL)
        self._record(
            claim.execution_id,
            [*path, _S.WAITING_APPROVAL] if claim.state is not _S.WAITING_APPROVAL else path[:1],
            result,
            request,
            definition,
            reason="approval_requested",
            approval_id=approval_id,
            now=now,
            final=False,
        )
        return result

    def _deny(
        self,
        claim: Claim,
        request: ActionRequest,
        refusal: Refusal,
        path: list[ActionState],
        now: datetime,
    ) -> ActionResult:
        result = self._result(claim.execution_id, ActionOutcome.DENIED, error=refusal.code)
        definition = self._p.catalog.actions.get(request.action.name)
        self._record(
            claim.execution_id,
            [*path, _S.DENIED],
            result,
            request,
            definition,
            reason=refusal.reason,
            now=now,
        )
        return result

    async def _run(
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        path: list[ActionState],
    ) -> ActionResult:
        chosen = self._route(request, definition)
        if chosen is None:
            return self._deny(
                claim,
                request,
                Refusal("no_channel", f"no deployed channel can run {definition.name}"),
                path,
                self._p.clock(),
            )
        binding, adapter = chosen
        path += [_S.ROUTED, _S.RUNNING]
        try:
            outcome = await adapter.execute(request, binding)
            verified = await self._verify(request, definition, adapter, outcome)
        except Exception as error:
            result = self._result(
                claim.execution_id, ActionOutcome.FAILED, channel=binding.channel,
                error=describe_error(error)[:64],
            )  # fmt: skip
            reason = describe_error(error)
            self._record(claim.execution_id, [*path, _S.FAILED], result, request, definition,
                         reason=reason, now=self._p.clock(), final=False)  # fmt: skip
            return result
        return self._finish_run(claim, request, definition, binding, outcome, verified, path)

    def _finish_run(  # noqa: PLR0913, PLR0917 - the run and what it produced
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        binding: ChannelBinding,
        outcome: ChannelOutcome,
        verified: bool | None,
        path: list[ActionState],
    ) -> ActionResult:
        now = self._p.clock()
        if outcome.handed_to_human:
            result = self._result(
                claim.execution_id, ActionOutcome.HANDED_TO_HUMAN, channel=binding.channel
            )
            self._record(claim.execution_id, [*path, _S.HANDED_TO_HUMAN], result, request,
                         definition, reason="handed_to_human", now=now, final=False)  # fmt: skip
            return result
        if definition.verify is not VerifyMode.NONE:
            path.append(_S.VERIFYING)
        if verified is False:
            result = self._result(
                claim.execution_id, ActionOutcome.FAILED, channel=binding.channel,
                error="verification",
            )  # fmt: skip
            self._record(claim.execution_id, [*path, _S.FAILED], result, request, definition,
                         reason="verification failed", now=now, final=False)  # fmt: skip
            return result
        result = ActionResult(
            execution_id=claim.execution_id,
            outcome=ActionOutcome.SUCCEEDED,
            attempts=1,
            channel=binding.channel,
            output=dict(outcome.output),
            verified=bool(verified),
        )
        self._record(claim.execution_id, [*path, _S.SUCCEEDED], result, request, definition,
                     now=now)  # fmt: skip
        return result

    def _route(
        self, request: ActionRequest, definition: ActionDefinition
    ) -> tuple[ChannelBinding, ChannelAdapter] | None:
        for binding in definition.bindings:
            adapter = self._p.adapters.get(binding.channel)
            if adapter is None:
                continue
            if request.actor.kind is ActorKind.AUTOMATION and not binding.deterministic:
                continue
            if binding.requires_capability is not None and (
                request.shop_id is None
                or not self._p.capabilities.available(request.shop_id, binding.requires_capability)
            ):
                continue
            return binding, adapter
        return None

    async def _verify(
        self,
        request: ActionRequest,
        definition: ActionDefinition,
        adapter: ChannelAdapter,
        outcome: ChannelOutcome,
    ) -> bool | None:
        if definition.verify is VerifyMode.REREAD:
            return await adapter.verify(request, outcome)
        if definition.verify is VerifyMode.SCHEMA:
            return not validate(dict(outcome.output), definition.output_schema)
        return None

    def _result(
        self,
        execution_id: uuid.UUID,
        outcome: ActionOutcome,
        *,
        channel: Channel | None = None,
        error: str | None = None,
    ) -> ActionResult:
        return ActionResult(
            execution_id=execution_id, outcome=outcome, attempts=0, channel=channel,
            error_code=error,
        )  # fmt: skip

    def _record(  # noqa: PLR0913 - one argument per part of the record
        self,
        execution_id: uuid.UUID,
        path: Sequence[ActionState],
        result: ActionResult,
        request: ActionRequest,
        definition: ActionDefinition | None,
        *,
        now: datetime,
        reason: str | None = None,
        approval_id: uuid.UUID | None = None,
        final: bool = True,
    ) -> None:
        action_path(*path)
        audited = definition is not None and definition.approval_policy.audited
        audit = None
        if audited and result.outcome is not ActionOutcome.DEFERRED:
            audit = AuditRecord.new(
                organization_id=request.organization_id,
                actor=request.actor,
                action=f"action.{result.outcome.value}",
                entity_type="action_execution",
                entity_id=str(execution_id),
                after={
                    "action": request.action.name,
                    "version": request.action.version,
                    "error_code": result.error_code,
                    "shop_id": None if request.shop_id is None else str(request.shop_id),
                },
                correlation_id=request.correlation_id,
            )
        event = None
        if final or result.outcome is ActionOutcome.FAILED:
            event = OutboxMessage(
                organization_id=request.organization_id,
                shop_id=request.shop_id,
                destination=EVENT_DESTINATION,
                idempotency_key=f"{EXECUTED_EVENT}:{execution_id}:{result.outcome.value}",
                payload=_event_payload(execution_id, request, result),
                action_execution_id=execution_id,
                correlation_id=request.correlation_id,
            )
        self._p.store.record(
            execution_id,
            Finish(
                state=path[-1],
                path=tuple(path),
                result=result,
                reason=None if reason is None else reason[:500],
                approval_id=approval_id,
                audit=audit,
                event=event,
                store_output=definition is not None
                and definition.approval_policy is not ApprovalPolicy.R1,
            ),
            now=now,
        )


def _event_payload(
    execution_id: uuid.UUID, request: ActionRequest, result: ActionResult
) -> dict[str, JsonValue]:
    """IDs and outcome only (EVENT_CATALOG rule 3): never the input or the output."""
    return {
        "event_type": EXECUTED_EVENT,
        "action_execution_id": str(execution_id),
        "action": request.action.name,
        "version": request.action.version,
        "outcome": result.outcome.value,
        "error_code": result.error_code,
        "actor": str(request.actor),
        "causation_id": request.causation_id,
    }
