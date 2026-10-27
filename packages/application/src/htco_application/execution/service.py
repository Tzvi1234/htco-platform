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
5. **Route** through the Execution Router (slice 5.13, `router.py`): the first binding, in
   order, that can run now; a refusal is `DENIED` with `scope_missing`, `environment_not_ready`,
   `flag_off` or `no_channel`. A channel that hands the action to a person (a security challenge,
   the Human channel) creates one task and the action is `HANDED_TO_HUMAN`; a resubmission after
   the task is completed checks the person's result against the output schema.
6. **Guard** (slice 5.15, `guards.py`): circuit breaker, then rate budget, then the concurrency
   slot; any of them busy is `DEFERRED` with `retry_at`, and a resubmission runs every check again.
7. **Execute** through the channel adapter, with bounded retries inside the attempt (each call
   passes the breaker and the budget again), then **verify** (re-read, schema or none).
8. **Record**: the action execution's final state and result, an audit record for writes and
   personal data, and `actions.executed.v1` through the outbox, in one transaction (AC-5.12.4).

Every refusal is a result with a stable `error_code` and a reason that never contains the input
(it may be personal) or a secret.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Protocol

from htco_application.access.policy import Outcome, Principal, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.execution.channels import (
    HUMAN_CHANNEL,
    BrowserRecipeError,
    HumanTasks,
    HumanTaskState,
)
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
from htco_application.execution.guards import Guards, InlineRetry
from htco_application.execution.router import (
    BrowserGate,
    ExecutionRouter,
    NoBrowser,
    NoRoute,
    ShopScopes,
    WriteSwitch,
)
from htco_application.execution.schema import validate
from htco_application.jobs import JsonValue, describe_error
from htco_application.marketplace import (
    MarketplaceUnavailableError,
    MarketplaceWriteRefusedError,
    QuotaDeferredError,
)
from htco_application.messaging import OutboxMessage
from htco_application.workforce.registry import Registry
from htco_application.workforce.tool_access import ToolVerdict, decide_tool_access
from htco_domain.actions import ActionState, ApprovalPolicy, VerifyMode, action_path
from htco_domain.actors import ActorKind
from htco_domain.guards import day_window
from htco_domain.identity import Role
from htco_domain.workforce import PRODUCTION_READINESS

EVENT_DESTINATION = "events.publish"
EXECUTED_EVENT = "actions.executed.v1"
# Every recorded status change publishes exactly one event (slice 5.18, AC-5.18.2): a result is
# `actions.executed.v1`; a step that waits says what it waits for.
APPROVAL_REQUIRED_EVENT = "actions.approval_required.v1"
HANDED_TO_HUMAN_EVENT = "actions.handed_to_human.v1"
DEFERRED_EVENT = "actions.deferred.v1"
_WAITING_EVENTS = {
    ActionState.WAITING_APPROVAL: APPROVAL_REQUIRED_EVENT,
    ActionState.HANDED_TO_HUMAN: HANDED_TO_HUMAN_EVENT,
    ActionState.DEFERRED: DEFERRED_EVENT,
}

_S = ActionState
# A claimed action that a resubmission continues: every check runs again.
_RESUMABLE = frozenset({_S.WAITING_APPROVAL, _S.FAILED, _S.DEFERRED})
# Without guards: one call, no inline retries.
_ONE_CALL = InlineRetry(max_attempts=1, backoff_seconds=())
_CLIENT_ERRORS = range(400, 500)
_TOO_MANY_REQUESTS = 429


_SERVER_ERRORS = 500
# Deferrals whose wait nothing else re-checks (a 429's Retry-After, the daily quota) or that end
# at a known time: a resubmission before retry_at sends nothing. A busy slot is checked again.
_HELD_UNTIL_RETRY_AT = frozenset({"rate_limited", "breaker_open"})
_TRANSPORT_ERRORS = (MarketplaceUnavailableError, ConnectionError, TimeoutError)


class _NoTaskStoreError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"handed to a person ({reason}) without a task store")


class _Failure(Enum):
    RATE_LIMITED = auto()  # 429: wait for Retry-After
    QUOTA = auto()  # the daily budget floor: the next UTC day
    PERMANENT = auto()  # another 4xx, or a local error: never retried, not a breaker failure
    TRANSIENT = auto()  # 5xx or a transport failure: retried for reads, a breaker failure


def _classify(error: Exception) -> tuple[_Failure, float | None]:
    if isinstance(error, QuotaDeferredError):
        return _Failure.QUOTA, None
    if isinstance(error, (MarketplaceWriteRefusedError, BrowserRecipeError)):
        return _Failure.PERMANENT, None  # refused before sending, or a recipe that cannot work
    status = getattr(error, "status", None)
    if status == _TOO_MANY_REQUESTS:
        wait = getattr(error, "retry_after_seconds", None)
        return _Failure.RATE_LIMITED, None if wait is None else float(wait)
    if isinstance(status, int) and status in _CLIENT_ERRORS:
        return _Failure.PERMANENT, None
    if (isinstance(status, int) and status >= _SERVER_ERRORS) or isinstance(
        error, _TRANSPORT_ERRORS
    ):
        return _Failure.TRANSIENT, None
    # A bug or an unexpected local error: running it again cannot help, and it says nothing
    # about the channel's health.
    return _Failure.PERMANENT, None


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
    retry_at: datetime | None = None  # a deferred action runs again from then on
    error_code: str | None = None  # why it was deferred or failed
    request_hash: str | None = None  # of the request that created it
    channel: Channel | None = None  # the channel it ran on, if it ran


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

    def executed(self, approval_id: uuid.UUID, *, execution_id: uuid.UUID) -> None:
        """The approved request succeeded as this execution: it never runs again (5.14)."""
        ...


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
    guards: Guards | None = None  # breakers, limits and retries (slice 5.15)
    browser: BrowserGate = field(default_factory=NoBrowser)  # environments, flags, scopes (5.13)
    human: HumanTasks | None = None  # hand-offs to a person (slice 5.13; Postgres in Phase 7)
    scopes: ShopScopes | None = None  # the shops' granted Etsy scopes (ADR-0022 §4)
    # The writes switched on, by action (W2's flag): a set, or a switch read at every check.
    write_flags: frozenset[str] | WriteSwitch = frozenset()


class Refusal(Exception):  # noqa: N818 - a refusal is the result, not a failure of the gateway
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class ActionGatewayService:
    def __init__(self, parts: GatewayParts) -> None:
        if parts.guards is not None:
            missing = sorted(
                {d.rate_limit_key for d in parts.catalog.actions.values() if d.rate_limit_key}
                - set(parts.catalog.rate_budgets)
            )
            if missing:
                raise ValueError(f"no rate budget for {', '.join(missing)}")
        if parts.human is None and {Channel.BROWSER, Channel.HUMAN} & set(parts.adapters):
            raise ValueError("a browser or human channel needs a task store for its hand-offs")
        unbuilt = sorted(
            f"{definition.name} -> {binding.channel.value}:{binding.recipe}"
            for definition in parts.catalog.actions.values()
            for binding in definition.bindings
            if not binding.disabled
            and binding.channel in parts.adapters
            and (recipes := getattr(parts.adapters[binding.channel], "recipes", None)) is not None
            and binding.recipe not in recipes
        )
        if unbuilt:
            # A recipe the channel cannot run would fail every call (AUD-06, #147): refuse to
            # start, unless the binding is marked disabled until it is built.
            raise ValueError(f"no implementation for {', '.join(unbuilt)}")
        self._p = parts
        self._router = ExecutionRouter(
            parts.adapters,
            parts.capabilities,
            parts.browser,
            scopes=parts.scopes,
            write_flags=parts.write_flags,
        )

    async def submit(self, request: ActionRequest) -> ActionResult:
        now = self._p.clock()
        digest = request_hash(request)
        claim = self._p.store.claim(request, request_hash=digest, now=now)
        early = self._answer_from_claim(claim, request, digest, now)
        if early is not None:
            return early
        # A resubmission without its approval (an automation resumed after the decision, slice
        # 5.14) uses the approval requested for this very request; the hash still has to match.
        implicit = request.approval_id is None and claim.approval_id is not None
        if implicit:
            request = replace(request, approval_id=claim.approval_id)
        path: list[ActionState] = [claim.state]
        try:
            definition = self._definition(request)
            forced_approval = self._authorize(request, definition)
            path.append(_S.AUTHORIZED)
            if (definition.approval_policy.is_write or forced_approval) and not (
                self._approved(request, digest, implicit=implicit)
            ):
                return self._wait_for_approval(claim, request, definition, digest, path, now)
        except Refusal as refusal:
            return self._deny(claim, request, refusal, path, now)
        return await self._run(claim, request, definition, path)

    def _answer_from_claim(
        self, claim: Claim, request: ActionRequest, digest: str, now: datetime
    ) -> ActionResult | None:
        """What a claim answers without running the checks again, or None to run them."""
        if claim.result is not None:
            return claim.result  # finished: the first result (AC-5.12.1)
        if not claim.created and claim.request_hash not in {None, digest}:
            # The same key for another request (actor, shop, input...): it never continues,
            # resumes or finishes the first one. Nothing is recorded.
            return self._result(
                claim.execution_id, ActionOutcome.DENIED, error="idempotency_conflict"
            )
        if claim.state is _S.HANDED_TO_HUMAN:
            return self._resume_from_human(claim, request)
        if not claim.created and claim.state not in _RESUMABLE:
            return self._result(claim.execution_id, ActionOutcome.DEFERRED, error="in_progress")
        if (
            claim.state is _S.DEFERRED
            and claim.error_code in _HELD_UNTIL_RETRY_AT
            and claim.retry_at is not None
            and now < claim.retry_at
        ):
            return replace(  # not yet: nothing is sent before retry_at (a 429, the daily quota)
                self._result(claim.execution_id, ActionOutcome.DEFERRED, error="not_yet"),
                retry_at=claim.retry_at,
            )
        return None

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
            if definition.approval_policy.is_write and (
                automation.readiness not in PRODUCTION_READINESS
            ):
                # The kill switch at execution time (sage, #126): a draft automation's queued or
                # retried run reaches here again, and its write stops.
                raise Refusal("automation_not_ready", f"{request.actor.id} is not ready")
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
        outcome = decide(
            principal, definition.user_capability, shop=None, now=self._p.clock()
        ).outcome
        if outcome is Outcome.STEP_UP_REQUIRED:
            raise Refusal("step_up_required", f"{definition.name} needs a recent step-up")
        if outcome is Outcome.MFA_REQUIRED:
            raise Refusal("mfa_required", f"{definition.name} needs a second factor")
        if outcome is not Outcome.ALLOW:
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

    def _approved(self, request: ActionRequest, digest: str, *, implicit: bool) -> bool:
        """The deciders' roles, step-up and expiry are the approval service's rules (5.14)."""
        if request.owner_trigger and request.actor.kind is ActorKind.USER:
            return True  # `_authorize_user` checked that the Owner triggered it
        if request.approval_id is None:
            return False
        if not self._p.approvals.is_approved(request.approval_id, request_hash=digest):
            if implicit:
                return False  # not decided yet: the request keeps waiting for it
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
        route = self._router.choose(definition, request)
        if isinstance(route, NoRoute):
            return self._deny(
                claim, request, Refusal(route.code, route.reason), path, self._p.clock()
            )
        binding, adapter = route.binding, route.adapter
        path.append(_S.ROUTED)
        guards = self._p.guards
        deferral = self._admit(definition, binding)
        if deferral is not None:
            return self._defer(claim, request, definition, path, *deferral)
        if definition.approval_policy.is_write and not self._router.write_enabled(definition.name):
            # Checked again right before the send (sage, #126): a flag turned off while the action
            # waited for its approval or its turn stops it.
            return self._deny(
                claim, request, Refusal("flag_off", f"the write flag of {definition.name} is off"),
                path, self._p.clock(),
            )  # fmt: skip
        slot = self._slot_key(request, definition) if guards is not None else None
        if guards is not None and slot is not None:
            now = self._p.clock()
            retry_at = guards.slots.acquire(
                slot,
                limit=guards.slot_limit,
                holder=claim.execution_id,
                lease_until=now + definition.timeout + guards.slot_margin,
                now=now,
            )
            if retry_at is not None:
                return self._defer(claim, request, definition, path, "slot_busy", retry_at)
        try:
            # The whole execution is bounded by the action's timeout, so a slot's lease (the
            # timeout plus a margin) always outlives the call it protects.
            async with asyncio.timeout(definition.timeout.total_seconds()):
                return await self._attempts(claim, request, definition, binding, adapter, path)
        except TimeoutError as error:
            self._breaker(binding, failed=True)
            run = path if path[-1] is _S.RUNNING else [*path, _S.RUNNING]
            return self._fail(claim, request, definition, binding, run, error, 1)
        finally:
            if guards is not None and slot is not None:
                guards.slots.release(slot, holder=claim.execution_id)

    async def _attempts(  # noqa: PLR0913, PLR0917 - the run and its channel
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        binding: ChannelBinding,
        adapter: ChannelAdapter,
        path: list[ActionState],
    ) -> ActionResult:
        """Call the channel with bounded retries; each call after the first is guarded again."""
        guards = self._p.guards
        retry = _ONE_CALL if guards is None else guards.retry
        path.append(_S.RUNNING)
        attempts = 0
        while True:
            if attempts:
                deferral = self._admit(definition, binding)
                if deferral is not None:
                    return self._defer(claim, request, definition, path, *deferral,
                                       attempts=attempts, channel=binding.channel)  # fmt: skip
            attempts += 1
            try:
                outcome = await adapter.execute(request, binding)
            except Exception as error:
                kind, wait = _classify(error)
                more = attempts < retry.max_attempts
                if kind is _Failure.RATE_LIMITED:
                    if (
                        guards is not None
                        and more
                        and wait is not None
                        and (timedelta(seconds=wait) <= retry.max_inline_wait)
                    ):
                        await guards.sleep(wait)
                        continue
                    delay = retry.unknown_rate_wait if wait is None else timedelta(seconds=wait)
                    return self._defer(claim, request, definition, path, "rate_limited",
                                       self._p.clock() + delay, attempts=attempts,
                                       channel=binding.channel)  # fmt: skip
                if kind is _Failure.QUOTA:
                    return self._defer(claim, request, definition, path, "rate_limited",
                                       day_window(self._p.clock())[1], attempts=attempts,
                                       channel=binding.channel)  # fmt: skip
                self._breaker(binding, failed=kind is _Failure.TRANSIENT)
                if (
                    guards is not None
                    and more
                    and kind is _Failure.TRANSIENT
                    and not definition.approval_policy.is_write  # never resend a write blindly
                ):
                    await guards.sleep(retry.backoff(attempts))
                    continue
                return self._fail(claim, request, definition, binding, path, error, attempts)
            self._breaker(binding, failed=False)
            break
        try:
            verified = await self._verify(request, definition, adapter, outcome)
        except Exception as error:
            return self._fail(claim, request, definition, binding, path, error, attempts)
        return self._finish_run(claim, request, definition, binding, outcome, verified, path,
                                attempts)  # fmt: skip

    def _admit(
        self, definition: ActionDefinition, binding: ChannelBinding
    ) -> tuple[str, datetime] | None:
        """The breaker, then the rate budget: None, or the reason code and `retry_at`."""
        guards = self._p.guards
        if guards is None:
            return None
        now = self._p.clock()
        if binding.breaker_key is not None:
            retry_at = guards.breakers.admit(binding.breaker_key, now=now)
            if retry_at is not None:
                return "breaker_open", retry_at
        if definition.rate_limit_key is not None:
            budget = self._p.catalog.rate_budgets[definition.rate_limit_key]
            retry_at = guards.rates.acquire(budget, now=now)
            if retry_at is not None:
                return "rate_limited", retry_at
        return None

    def _breaker(self, binding: ChannelBinding, *, failed: bool) -> None:
        if self._p.guards is not None and binding.breaker_key is not None:
            self._p.guards.breakers.record(binding.breaker_key, failed=failed, now=self._p.clock())

    @staticmethod
    def _slot_key(request: ActionRequest, definition: ActionDefinition) -> str | None:
        if definition.concurrency_key is None:
            return None
        return definition.concurrency_key.format(
            shop_id=request.shop_id, organization_id=request.organization_id
        )

    def _defer(  # noqa: PLR0913, PLR0917 - the run, the reason and when to try again
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        path: list[ActionState],
        code: str,
        retry_at: datetime,
        *,
        attempts: int = 0,
        channel: Channel | None = None,
    ) -> ActionResult:
        result = ActionResult(
            execution_id=claim.execution_id, outcome=ActionOutcome.DEFERRED, attempts=attempts,
            channel=channel, error_code=code, retry_at=retry_at,
        )  # fmt: skip
        self._record(claim.execution_id, [*path, _S.DEFERRED], result, request, definition,
                     reason=code, now=self._p.clock(), final=False)  # fmt: skip
        return result

    def _fail(  # noqa: PLR0913, PLR0917 - the run and what went wrong
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        binding: ChannelBinding,
        path: list[ActionState],
        error: Exception,
        attempts: int,
        code: str | None = None,
    ) -> ActionResult:
        result = replace(
            self._result(claim.execution_id, ActionOutcome.FAILED, channel=binding.channel,
                         error=code or describe_error(error)[:64]),
            attempts=attempts,
        )  # fmt: skip
        if definition.approval_policy is ApprovalPolicy.W3:
            # One press or one approval, one send at most: the failure is final for this key, and
            # the next request (with its own key) runs every check again.
            self._record(claim.execution_id, [*path, _S.FAILED, _S.DEAD_LETTERED], result,
                         request, definition, reason=describe_error(error),
                         now=self._p.clock())  # fmt: skip
            return result
        self._record(claim.execution_id, [*path, _S.FAILED], result, request, definition,
                     reason=describe_error(error), now=self._p.clock(), final=False)  # fmt: skip
        return result

    def _finish_run(  # noqa: PLR0913, PLR0917 - the run and what it produced
        self,
        claim: Claim,
        request: ActionRequest,
        definition: ActionDefinition,
        binding: ChannelBinding,
        outcome: ChannelOutcome,
        verified: bool | None,
        path: list[ActionState],
        attempts: int = 1,
    ) -> ActionResult:
        now = self._p.clock()
        if outcome.handed_to_human:
            reason = outcome.handoff_reason or HUMAN_CHANNEL
            if self._p.human is None:  # no one could ever pick it up: fail, never strand it
                return self._fail(claim, request, definition, binding, path,
                                  _NoTaskStoreError(reason), attempts,
                                  code="no_task_store")  # fmt: skip
            self._p.human.hand_off(claim.execution_id, request, reason=reason)
            result = replace(
                self._result(claim.execution_id, ActionOutcome.HANDED_TO_HUMAN,
                             channel=binding.channel),
                attempts=attempts,
            )  # fmt: skip
            self._record(claim.execution_id, [*path, _S.HANDED_TO_HUMAN], result, request,
                         definition, reason=reason, now=now, final=False)  # fmt: skip
            return result
        if definition.verify is not VerifyMode.NONE:
            path.append(_S.VERIFYING)
        if verified is False:
            result = replace(
                self._result(claim.execution_id, ActionOutcome.FAILED, channel=binding.channel,
                             error="verification"),
                attempts=attempts,
            )  # fmt: skip
            if definition.approval_policy is ApprovalPolicy.W3:
                # The write may have reached Etsy (quill on #161): never resent under this key.
                self._record(claim.execution_id, [*path, _S.FAILED, _S.DEAD_LETTERED], result,
                             request, definition, reason="verification failed",
                             now=now)  # fmt: skip
                return result
            self._record(claim.execution_id, [*path, _S.FAILED], result, request, definition,
                         reason="verification failed", now=now, final=False)  # fmt: skip
            return result
        result = ActionResult(
            execution_id=claim.execution_id,
            outcome=ActionOutcome.SUCCEEDED,
            attempts=attempts,
            channel=binding.channel,
            output=dict(outcome.output),
            verified=bool(verified),
        )
        self._record(claim.execution_id, [*path, _S.SUCCEEDED], result, request, definition,
                     now=now)  # fmt: skip
        return result

    def _resume_from_human(self, claim: Claim, request: ActionRequest) -> ActionResult:
        """A handed-off action continues once its task is decided; until then it waits."""
        task = None if self._p.human is None else self._p.human.task_of(claim.execution_id)
        definition = self._p.catalog.get(request.action.name, request.action.version)
        waiting = ActionResult(execution_id=claim.execution_id,
                               outcome=ActionOutcome.HANDED_TO_HUMAN, attempts=0,
                               channel=claim.channel)  # fmt: skip
        if task is None or task.state is HumanTaskState.OPEN or definition is None:
            return waiting
        now = self._p.clock()
        if task.state is HumanTaskState.CANCELLED:
            result = replace(waiting, outcome=ActionOutcome.FAILED, error_code="cancelled")
            self._record(claim.execution_id, [_S.HANDED_TO_HUMAN, _S.CANCELLED], result, request,
                         definition, reason="cancelled by a person", now=now)  # fmt: skip
            return result
        output = dict(task.output or {})
        path = [_S.HANDED_TO_HUMAN, _S.RUNNING, _S.VERIFYING]
        if validate(output, definition.output_schema):
            result = replace(waiting, outcome=ActionOutcome.FAILED, error_code="verification")
            self._record(claim.execution_id, [*path, _S.FAILED], result, request, definition,
                         reason="the person's result does not match the output contract",
                         now=now, final=False)  # fmt: skip
            return result
        result = replace(waiting, outcome=ActionOutcome.SUCCEEDED, output=output, verified=True)
        self._record(claim.execution_id, [*path, _S.SUCCEEDED], result, request, definition,
                     now=now)  # fmt: skip
        return result

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
        event_type = (
            EXECUTED_EVENT
            if final or result.outcome is ActionOutcome.FAILED
            else _WAITING_EVENTS.get(path[-1])
        )
        if event_type is not None and len(path) > 1:  # a step that changed the status
            event = OutboxMessage(
                organization_id=request.organization_id,
                shop_id=request.shop_id,
                destination=EVENT_DESTINATION,
                # One message per recorded step, even two at the same instant.
                idempotency_key=f"{event_type}:{execution_id}:{uuid.uuid7()}",
                payload=_event_payload(execution_id, request, result, event_type),
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
        if final and result.outcome is ActionOutcome.SUCCEEDED and request.approval_id is not None:
            self._p.approvals.executed(request.approval_id, execution_id=execution_id)


def _event_payload(
    execution_id: uuid.UUID, request: ActionRequest, result: ActionResult, event_type: str
) -> dict[str, JsonValue]:
    """IDs and outcome only (EVENT_CATALOG rule 3): never the input or the output."""
    return {
        "event_type": event_type,
        "action_execution_id": str(execution_id),
        "action": request.action.name,
        "version": request.action.version,
        "outcome": result.outcome.value,
        "error_code": result.error_code,
        "actor": str(request.actor),
        "causation_id": request.causation_id,
    }
