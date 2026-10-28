"""The shared approval contract (slice 5.14): the in-memory service, its decision rules, the
gateway's use of it (AC-5.14.1, AC-5.14.2) and a waiting automation's resume (AC-5.14.3)."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_application.access.authentication import AuthContext, Membership
from htco_application.access.policy import Principal
from htco_application.approvals import (
    APPROVAL_DECIDED,
    AlreadyExecutedError,
    ApprovalNotFoundError,
    ApprovalResumer,
    DecisionRefusedError,
    GatewayApprovals,
    MemoryApprovalService,
    NoApprovals,
    approval_request_for,
    decision_event,
)
from htco_application.approvals import (
    AlreadyExecutedError as NeverApprovedError,
)
from htco_application.automation.durable import (
    Completed,
    DurableAutomationEngine,
    HandlerContext,
    HandlerOutcome,
    Waiting,
)
from htco_application.automation.engine import TriggerRequest
from htco_application.execution.gateway import ActionOutcome, ActionRef, ActionRequest, Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts, request_hash
from htco_application.kernel import event_payload, read_event
from htco_application.messaging import PendingDelivery
from htco_application.notifications import EventDeliverer
from htco_application.workforce.files import read_registry
from htco_application.workforce.registry import Registry, parse_automation
from htco_domain.actions import ApprovalPolicy
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.approvals import ApprovalState, Decision
from htco_domain.automation import ExecutionState, TriggerKind
from htco_domain.identity import AssuranceLevel, Role
from tests.automation_fakes import MemoryExecutionStore, engine_document
from tests.event_contracts import assert_event_contract
from tests.execution_fakes import (
    REGISTRY,
    Capabilities,
    FakeChannel,
    MemoryActionStore,
    Principals,
    Shops,
    real_catalog,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
ORG = uuid.UUID("6a000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("6b000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("6a300000-0000-0000-0000-000000000001")
OWNER = uuid.UUID("6a100000-0000-0000-0000-000000000001")
ADMIN = uuid.UUID("6a100000-0000-0000-0000-000000000002")
REQUESTER = uuid.UUID("6a100000-0000-0000-0000-000000000003")
INTAKE = "etsy-order-intake"
WRITE_INPUT: dict[str, Any] = {"listing_id": 1, "products": []}


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def person(
    user: uuid.UUID = OWNER,
    role: Role = Role.OWNER,
    *,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    reauthenticated_at: datetime | None = NOW,
    organization: uuid.UUID = ORG,
) -> AuthContext:
    return AuthContext(
        user_id=user, assurance=assurance, session_id=None,
        memberships=(Membership(organization, role, frozenset()),),
        reauthenticated_at=reauthenticated_at,
    )  # fmt: skip


def office() -> Registry:
    real = read_registry(REGISTRY).registry()
    automation = parse_automation(
        engine_document(INTAKE, allowed_actions=["etsy.inventory.update"])
    )
    return Registry(
        employees=MappingProxyType({}),
        tools=real.tools,
        automations=MappingProxyType({INTAKE: automation}),
        advisors=MappingProxyType({}),
    )


@dataclass
class World:
    clock: Clock = field(default_factory=Clock)
    channel: FakeChannel = field(default_factory=lambda: FakeChannel(output={"listing_id": 1}))

    def __post_init__(self) -> None:
        self.registry = office()
        self.approvals = MemoryApprovalService(self.clock)
        self.store = MemoryActionStore()
        self.gateway = ActionGatewayService(
            GatewayParts(
                catalog=real_catalog(self.registry, built=True),
                registry=lambda: self.registry,
                store=self.store,
                principals=Principals(
                    {
                        (REQUESTER, ORG): Principal(
                            organization_id=ORG,
                            role=Role.OWNER,
                            assurance=AssuranceLevel.AAL2,
                            reauthenticated_at=NOW,
                            granted_shops=frozenset({SHOP}),
                        )
                    }
                ),
                shops=Shops({SHOP: ORG}),
                approvals=GatewayApprovals(self.approvals, self.clock),
                adapters={Channel.API: self.channel},
                capabilities=Capabilities(),
                # These tests run the inventory write: its flag is on (writes are off by default).
                write_flags=frozenset({"etsy.inventory.update"}),
                clock=self.clock,
            )
        )

    def only_approval(self) -> uuid.UUID:
        [approval_id] = self.approvals.approvals
        return approval_id

    def approve(self, by: AuthContext | None = None) -> None:
        approval = self.approvals.status(self.only_approval())
        self.approvals.decide(
            approval.id, Decision.APPROVE, by=by or person(),
            shown_hash=approval.request.request_hash,
        )  # fmt: skip


def write(key: str = "inventory:1", **changes: Any) -> ActionRequest:
    values: dict[str, Any] = {
        "action": ActionRef("etsy.inventory.update", 1),
        "actor": ActorRef(ActorKind.USER, str(REQUESTER)),
        "organization_id": ORG,
        "shop_id": SHOP,
        "idempotency_key": key,
        "correlation_id": "corr-1",
        "input": WRITE_INPUT,
    } | changes
    return ActionRequest(**values)


def definition() -> Any:
    return real_catalog(office()).actions["etsy.inventory.update"]


def approval_of(request: ActionRequest, *, ttl: timedelta = timedelta(days=3)) -> Any:
    return approval_request_for(
        request, request_hash=request_hash(request), definition=definition(), now=NOW, ttl=ttl
    )


# --- AC-5.14.1: the hash covers everything sent --------------------------------------------------

_SENT = st.sampled_from(
    ["actor", "on_behalf_of_user", "organization_id", "shop_id", "key", "input"]
)


@pytest.mark.ac("AC-5.14.1")
@given(field_name=_SENT, value=st.integers(min_value=2, max_value=10**9))
def test_request_hash__any_sent_parameter_changes_it(field_name: str, value: int) -> None:
    request = write()
    table: dict[str, dict[str, Any]] = {
        "actor": {"actor": ActorRef(ActorKind.USER, str(uuid.UUID(int=value)))},
        "on_behalf_of_user": {"on_behalf_of_user": uuid.UUID(int=value)},
        "organization_id": {"organization_id": uuid.UUID(int=value)},
        "shop_id": {"shop_id": uuid.UUID(int=value)},
        "key": {"idempotency_key": f"inventory:{value}"},
        "input": {"input": {"listing_id": value, "products": []}},
    }
    changed: dict[str, Any] = table[field_name]

    assert request_hash(replace(request, **changed)) != request_hash(request)


@pytest.mark.ac("AC-5.14.1")
def test_request_hash__the_approval_itself_is_not_part_of_it() -> None:
    request = write()

    assert request_hash(replace(request, approval_id=uuid.uuid4())) == request_hash(request)


@pytest.mark.ac("AC-5.14.1")
async def test_gateway__a_resubmission_that_differs_from_the_approved_one__refused() -> None:
    world = World()
    await world.gateway.submit(write())
    world.approve()

    other = write(key="inventory:2", input={"listing_id": 2, "products": []})
    result = await world.gateway.submit(replace(other, approval_id=world.only_approval()))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "approval_invalid")
    assert world.channel.calls == []


@pytest.mark.ac("AC-5.14.1")
def test_decide__shown_another_hash__refused() -> None:
    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))

    with pytest.raises(DecisionRefusedError) as refused:
        service.decide(approval.id, Decision.APPROVE, by=person(), shown_hash="0" * 64)

    assert refused.value.code == "hash_mismatch"
    assert service.status(approval.id).state is ApprovalState.REQUESTED


def test_approval__parameters_are_what_will_be_sent() -> None:
    request = approval_of(write())

    assert dict(request.parameters) == WRITE_INPUT
    assert request.summary == "Run etsy.inventory.update v1"
    assert (request.policy, request.requires_step_up) == (ApprovalPolicy.W2, True)


# --- AC-5.14.2: at most once ---------------------------------------------------------------------


@pytest.mark.ac("AC-5.14.2")
async def test_gateway__an_approved_request_executes_at_most_once() -> None:
    world = World()
    pending = await world.gateway.submit(write())
    world.approve()
    approval_id = world.only_approval()

    done = await world.gateway.submit(replace(write(), approval_id=approval_id))
    again = await world.gateway.submit(replace(write(), approval_id=approval_id))

    assert pending.outcome is ActionOutcome.PENDING_APPROVAL
    assert done.outcome is ActionOutcome.SUCCEEDED
    assert again == done
    assert len(world.channel.calls) == 1
    approval = world.approvals.status(approval_id)
    assert (approval.state, approval.execution_id) == (ApprovalState.EXECUTED, done.execution_id)


@pytest.mark.ac("AC-5.14.2")
def test_executed__as_another_execution__refused() -> None:
    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))
    service.decide(approval.id, Decision.APPROVE, by=person(),
                   shown_hash=approval.request.request_hash)  # fmt: skip
    first = uuid.uuid4()
    service.executed(approval.id, execution_id=first)

    assert service.executed(approval.id, execution_id=first).execution_id == first
    with pytest.raises(AlreadyExecutedError):
        service.executed(approval.id, execution_id=uuid.uuid4())


@pytest.mark.ac("AC-5.14.2")
def test_executed__not_approved__refused() -> None:
    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))

    with pytest.raises(Exception, match="requested -> executed"):
        service.executed(approval.id, execution_id=uuid.uuid4())


# --- Decision rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("by", "code"),
    [
        (person(REQUESTER), "own_request"),
        (person(role=Role.OPERATIONS), "forbidden"),
        (person(assurance=AssuranceLevel.AAL1), "mfa_required"),
        (person(reauthenticated_at=None), "step_up_required"),
        (person(reauthenticated_at=NOW - timedelta(minutes=11)), "step_up_required"),
        (person(organization=OTHER_ORG), "not_found"),
    ],
)
def test_decide__the_rules(by: AuthContext, code: str) -> None:
    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))

    with pytest.raises(DecisionRefusedError) as refused:
        service.decide(approval.id, Decision.APPROVE, by=by,
                       shown_hash=approval.request.request_hash)  # fmt: skip

    assert refused.value.code == code


def test_decide__w3_by_an_admin__forbidden_by_the_owner__approved() -> None:
    service = MemoryApprovalService(Clock())
    w3 = replace(
        approval_of(write()), policy=ApprovalPolicy.W3, decided_by_roles=frozenset({Role.OWNER})
    )
    approval = service.request(w3)
    shown = approval.request.request_hash

    with pytest.raises(DecisionRefusedError):
        service.decide(
            approval.id, Decision.APPROVE, by=person(ADMIN, Role.ADMIN), shown_hash=shown
        )
    decided = service.decide(approval.id, Decision.APPROVE, by=person(), shown_hash=shown)

    assert (decided.state, decided.decided_by, decided.shown_hash) == (
        ApprovalState.APPROVED,
        OWNER,
        shown,
    )


def test_decide__twice__refused() -> None:
    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))
    shown = approval.request.request_hash
    service.decide(approval.id, Decision.REJECT, by=person(), shown_hash=shown)

    with pytest.raises(DecisionRefusedError) as refused:
        service.decide(approval.id, Decision.APPROVE, by=person(), shown_hash=shown)

    assert refused.value.code == "not_pending"


def test_status__past_its_time__expired_and_undecidable() -> None:
    clock = Clock()
    service = MemoryApprovalService(clock)
    approval = service.request(approval_of(write(), ttl=timedelta(hours=1)))
    clock.now = NOW + timedelta(hours=1)

    assert service.status(approval.id).state is ApprovalState.EXPIRED
    with pytest.raises(DecisionRefusedError):
        service.decide(approval.id, Decision.APPROVE, by=person(),
                       shown_hash=approval.request.request_hash)  # fmt: skip


def test_request__the_same_request_twice__one_open_approval() -> None:
    service = MemoryApprovalService(Clock())

    first = service.request(approval_of(write()))
    second = service.request(approval_of(write()))

    assert first.id == second.id


def test_status__unknown__not_found() -> None:
    with pytest.raises(ApprovalNotFoundError):
        MemoryApprovalService(Clock()).status(uuid.uuid4())


@pytest.mark.parametrize(
    "changes",
    [
        {"request_hash": "nope"},
        {"decided_by_roles": frozenset({Role.ADMIN})},
        {"requires_step_up": False},
        {"expires_at": datetime(2026, 10, 25)},  # noqa: DTZ001 - the naive time under test
        {"summary": " "},
        {"policy": ApprovalPolicy.R1},
    ],
)
def test_approval_request__inconsistent__refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        replace(approval_of(write()), **changes)


async def test_gateway__unknown_approval__invalid() -> None:
    world = World()

    result = await world.gateway.submit(replace(write(), approval_id=uuid.uuid4()))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "approval_invalid")


# --- AC-5.14.3: a waiting automation -------------------------------------------------------------


@dataclass
class WritingHandler:
    """An automation step that writes through the gateway, naming its execution as the cause."""

    outcomes: list[ActionOutcome] = field(default_factory=list)

    async def handle(self, context: HandlerContext) -> HandlerOutcome:
        result = await context.gateway.submit(
            ActionRequest(
                action=ActionRef("etsy.inventory.update", 1),
                actor=ActorRef(ActorKind.AUTOMATION, INTAKE),
                organization_id=context.organization_id,
                shop_id=context.shop_id,
                idempotency_key=f"inventory:{context.payload['receipt_id']}",
                correlation_id=context.correlation_id,
                causation_id=str(context.execution_id),
                input=WRITE_INPUT,
            )
        )
        self.outcomes.append(result.outcome)
        if result.outcome.waits:
            return Waiting(result.outcome.value, result.execution_id)
        return Completed({"listing_id": 1})


@dataclass
class TwoWritesHandler:
    """A step that writes twice, each write waiting for its own approval (Codex on #175)."""

    outcomes: list[ActionOutcome] = field(default_factory=list)

    async def handle(self, context: HandlerContext) -> HandlerOutcome:
        for key in ("inventory:first", "inventory:second"):
            result = await context.gateway.submit(
                ActionRequest(
                    action=ActionRef("etsy.inventory.update", 1),
                    actor=ActorRef(ActorKind.AUTOMATION, INTAKE),
                    organization_id=context.organization_id,
                    shop_id=context.shop_id,
                    idempotency_key=key,
                    correlation_id=context.correlation_id,
                    causation_id=str(context.execution_id),
                    input=WRITE_INPUT,
                )
            )
            self.outcomes.append(result.outcome)
            if result.outcome.waits:
                return Waiting(result.outcome.value, result.execution_id)
        return Completed({"listing_id": 1})


class Automations:
    def __init__(self, handler: WritingHandler | TwoWritesHandler | None = None) -> None:
        self.world = World()
        self.store = MemoryExecutionStore(shops={SHOP: ORG})
        self.handler = handler or WritingHandler()
        self.engine = DurableAutomationEngine(
            lambda: self.world.registry,
            self.store,
            {INTAKE: self.handler.handle},
            clock=self.world.clock,
            jitter=lambda: 1.0,
            gateway=self.world.gateway,
        )
        # The worker's `events.publish` with the decision's subscriber (AUD-03).
        self.events = EventDeliverer({APPROVAL_DECIDED: ApprovalResumer(self.engine)})
        self.delivered = 0

    async def deliver(self, *, again: bool = False) -> None:
        """Deliver the decisions written since the last call (`again`: replay them all)."""
        outbox = self.world.approvals.outbox
        for message in outbox if again else outbox[self.delivered :]:
            await self.events.deliver(PendingDelivery(uuid.uuid4(), message, 0, delivered=False))
        self.delivered = len(outbox)

    async def waiting(self) -> uuid.UUID:
        execution = self.engine.trigger(
            TriggerRequest(
                automation_id=INTAKE, trigger=TriggerKind.EVENT,
                actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"), idempotency_key="k-1",
                payload={"receipt_id": "77"}, correlation_id="corr-a", organization_id=ORG,
                shop_id=SHOP,
            )
        )  # fmt: skip
        waiting = await self.engine.execute(execution.id, worker=ActorRef(ActorKind.SYSTEM, "w"))
        assert waiting.state is ExecutionState.WAITING
        return execution.id

    def state(self, execution_id: uuid.UUID) -> ExecutionState:
        stored = self.store.load(execution_id)
        assert stored is not None
        return stored.execution.state

    def reasons(self) -> list[tuple[ExecutionState, str | None]]:
        return [(c.to_state, c.reason) for c in self.store.changes]


@pytest.mark.ac("AC-5.14.3")
async def test_waiting_automation__holds_no_job_and_resumes_once_when_approved() -> None:
    automations = Automations()
    execution_id = await automations.waiting()
    jobs_while_waiting = len(automations.store.jobs)

    automations.world.approve()
    await automations.deliver()
    resumed_jobs = len(automations.store.jobs)
    await automations.deliver(again=True)  # a duplicate notice
    done = await automations.engine.execute(execution_id, worker=ActorRef(ActorKind.SYSTEM, "w"))

    assert jobs_while_waiting == 1  # only the trigger's job: nothing is leased while it waits
    assert resumed_jobs == len(automations.store.jobs) == 2  # one resume, never two
    assert done.state is ExecutionState.SUCCEEDED
    assert automations.handler.outcomes == [ActionOutcome.PENDING_APPROVAL, ActionOutcome.SUCCEEDED]
    assert len(automations.world.channel.calls) == 1
    assert (ExecutionState.RETRY_SCHEDULED, "approval_approved") in automations.reasons()
    approval = automations.world.approvals.status(automations.world.only_approval())
    assert approval.request.waiting_execution_id == execution_id
    assert approval.state is ApprovalState.EXECUTED


@pytest.mark.ac("AC-5.14.3")
async def test_waiting_automation__rejected__cancelled_with_the_reason() -> None:
    automations = Automations()
    execution_id = await automations.waiting()
    approval = automations.world.approvals.status(automations.world.only_approval())

    automations.world.approvals.decide(approval.id, Decision.REJECT, by=person(),
                                       shown_hash=approval.request.request_hash)  # fmt: skip
    await automations.deliver()

    assert automations.state(execution_id) is ExecutionState.CANCELLED
    assert (ExecutionState.CANCELLED, "approval_rejected") in automations.reasons()
    assert automations.world.channel.calls == []


@pytest.mark.ac("AC-5.14.3")
async def test_waiting_automation__expired__cancelled_with_the_reason() -> None:
    automations = Automations()
    execution_id = await automations.waiting()

    automations.world.clock.now = NOW + timedelta(days=3)
    automations.world.approvals.status(automations.world.only_approval())
    await automations.deliver()

    assert automations.state(execution_id) is ExecutionState.CANCELLED
    assert (ExecutionState.CANCELLED, "approval_expired") in automations.reasons()


async def test_resumed_before_the_decision__the_action_still_waits() -> None:
    """A resubmission without its approval keeps waiting while the approval is open."""
    world = World()
    request = write()
    await world.gateway.submit(request)

    again = await world.gateway.submit(request)

    assert again.outcome is ActionOutcome.PENDING_APPROVAL
    assert len(world.approvals.approvals) == 1
    assert world.channel.calls == []


async def test_resumer__an_approval_without_a_waiting_execution__does_nothing() -> None:
    class Engine:
        calls = 0

        def resume(self, *_: object, **__: object) -> None:
            Engine.calls += 1

        def cancel_waiting(self, *_: object, **__: object) -> None:
            Engine.calls += 1

    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))
    service.decide(approval.id, Decision.APPROVE, by=person(),
                   shown_hash=approval.request.request_hash)  # fmt: skip
    await ApprovalResumer(Engine()).handle(service.outbox[-1])

    assert Engine.calls == 0


# --- AUD-03: decisions cross processes as events --------------------------------------------


@pytest.mark.ac("AUD-03.2")
async def test_a_decision__resumes_nothing_by_itself__only_its_event_does() -> None:
    automations = Automations()
    execution_id = await automations.waiting()

    automations.world.approve()

    assert automations.state(execution_id) is ExecutionState.WAITING  # no in-process listener
    [event] = automations.world.approvals.outbox
    assert event_payload(event)["waiting_execution_id"] == str(execution_id)
    await automations.deliver()
    assert automations.state(execution_id) is ExecutionState.RETRY_SCHEDULED


@pytest.mark.ac("AUD-03.2")
async def test_a_decision_through_another_service_instance__resumes_the_held_execution() -> None:
    """The API's service decides; the worker's engine holds the execution: one shared store
    (the database), two instances, and the event is the only link between them."""
    automations = Automations()
    execution_id = await automations.waiting()
    api = MemoryApprovalService(
        automations.world.clock, approvals=automations.world.approvals.approvals
    )
    approval = api.status(automations.world.only_approval())

    api.decide(approval.id, Decision.APPROVE, by=person(), shown_hash=approval.request.request_hash)

    assert automations.world.approvals.outbox == []  # the worker's instance heard nothing
    assert automations.state(execution_id) is ExecutionState.WAITING
    for message in (*api.outbox, *api.outbox):  # delivered, then replayed
        await automations.events.deliver(PendingDelivery(uuid.uuid4(), message, 0, delivered=False))
    done = await automations.engine.execute(execution_id, worker=ActorRef(ActorKind.SYSTEM, "w"))
    assert done.state is ExecutionState.SUCCEEDED
    assert [r for _, r in automations.reasons()].count("approval_approved") == 1


@pytest.mark.ac("AUD-03.2")
async def test_a_replayed_decision__never_resumes_a_later_wait() -> None:
    """Codex on #175: the first decision is delivered, the resumed run reaches a second approval
    and waits again, then the first decision is replayed (a crash before its acknowledgement).
    The replay names the first wait, so the second one stays waiting and no job is added."""
    automations = Automations(TwoWritesHandler())
    execution_id = await automations.waiting()
    automations.world.approve()
    await automations.deliver()
    worker = ActorRef(ActorKind.SYSTEM, "w")
    second = await automations.engine.execute(execution_id, worker=worker)
    assert second.state is ExecutionState.WAITING
    assert automations.handler.outcomes == [
        ActionOutcome.PENDING_APPROVAL, ActionOutcome.SUCCEEDED, ActionOutcome.PENDING_APPROVAL,
    ]  # fmt: skip
    jobs = len(automations.store.jobs)

    await automations.deliver(again=True)  # the first decision, replayed

    assert automations.state(execution_id) is ExecutionState.WAITING
    assert len(automations.store.jobs) == jobs
    assert [r for _, r in automations.reasons()].count("approval_approved") == 1


@pytest.mark.ac("AUD-03.2")
async def test_a_replayed_rejection__never_cancels_a_later_wait() -> None:
    """The same binding for an ending: a decision ends only the wait it was taken for."""
    automations = Automations(TwoWritesHandler())
    execution_id = await automations.waiting()
    automations.world.approve()
    await automations.deliver()
    await automations.engine.execute(execution_id, worker=ActorRef(ActorKind.SYSTEM, "w"))
    first = automations.world.approvals.outbox[0]
    stale = replace(
        first,
        payload={**first.payload, "payload": {**event_payload(first), "state": "rejected"}},
    )

    await automations.events.deliver(PendingDelivery(uuid.uuid4(), stale, 0, delivered=False))

    assert automations.state(execution_id) is ExecutionState.WAITING


@pytest.mark.ac("AUD-03.2")
def test_decision_event__approved_then_expired__two_events_with_their_own_keys() -> None:
    """Codex on #175: an approved approval that lapses before it runs is EXPIRED, and that
    second event must not share the first one's key, or the outbox (`on conflict
    (idempotency_key) do nothing`) drops it and the waiting execution is never cancelled."""
    clock = Clock()
    service = MemoryApprovalService(clock)
    approval = service.request(approval_of(write(), ttl=timedelta(hours=1)))
    service.decide(approval.id, Decision.APPROVE, by=person(),
                   shown_hash=approval.request.request_hash)  # fmt: skip
    clock.now = NOW + timedelta(hours=1)

    assert service.status(approval.id).state is ApprovalState.EXPIRED
    approved, expired = service.outbox
    assert [event_payload(m)["state"] for m in (approved, expired)] == ["approved", "expired"]
    assert approved.idempotency_key != expired.idempotency_key
    assert read_event(approved.payload).event_id != read_event(expired.payload).event_id
    # The same decision built again (a retried transaction) keeps its key and its event ID.
    again = decision_event(service.status(approval.id), at=clock.now)
    assert again.idempotency_key == expired.idempotency_key
    assert read_event(again.payload).event_id == read_event(expired.payload).event_id


@pytest.mark.ac("AUD-03.2")
@pytest.mark.parametrize("decision", [Decision.APPROVE, Decision.REJECT])
def test_decision_event__ids_and_outcome_only__matches_its_contract(decision: Decision) -> None:
    service = MemoryApprovalService(Clock())
    approval = service.request(approval_of(write()))

    decided = service.decide(approval.id, decision, by=person(),
                             shown_hash=approval.request.request_hash)  # fmt: skip

    [event] = service.outbox
    assert event == decision_event(decided, at=NOW)
    assert_event_contract(event)
    assert event.idempotency_key == f"{APPROVAL_DECIDED}:{approval.id}:{decided.state.value}"
    assert event_payload(event) == {
        "approval_id": str(approval.id),
        "state": decided.state.value,
        "action": "etsy.inventory.update",
        "action_version": 1,
        "waiting_execution_id": None,
        "action_execution_id": None,
        "decided_by": str(OWNER),
    }
    assert "listing_id" not in str(event.payload)  # never the parameters


@pytest.mark.ac("AUD-03.2")
def test_an_expiry__writes_its_decision_event_once() -> None:
    clock = Clock()
    service = MemoryApprovalService(clock)
    approval = service.request(approval_of(write()))
    clock.now = NOW + timedelta(days=4)

    service.status(approval.id)
    service.status(approval.id)

    [event] = service.outbox
    assert_event_contract(event)
    assert event_payload(event)["state"] == "expired"
    assert event_payload(event)["decided_by"] is None


def test_decision_event__an_open_approval__is_refused() -> None:
    approval = MemoryApprovalService(Clock()).request(approval_of(write()))

    with pytest.raises(ValueError, match="not decided"):
        decision_event(approval, at=NOW)


@pytest.mark.ac("AUD-03.2")
def test_the_service__has_no_in_process_listener() -> None:
    assert "listeners" not in MemoryApprovalService.__dataclass_fields__


# --- AUD-03 AC-3: the worker's gate ------------------------------------------------------------


@pytest.mark.ac("AUD-03.3")
async def test_no_approvals__a_write_waits_and_runs_nothing() -> None:
    world = World()
    gate = NoApprovals()
    world.gateway = ActionGatewayService(replace(world.gateway._p, approvals=gate))

    first = await world.gateway.submit(write())
    again = await world.gateway.submit(write())

    assert first.outcome is ActionOutcome.PENDING_APPROVAL
    assert again.outcome is ActionOutcome.PENDING_APPROVAL
    assert world.channel.calls == []
    [approval_id] = gate.pending
    assert gate.is_approved(approval_id, request_hash=gate.pending[approval_id]) is False


@pytest.mark.ac("AUD-03.3")
def test_no_approvals__never_approves_and_never_binds_an_execution() -> None:
    gate = NoApprovals()
    approval_id = gate.request(
        write(),
        request_hash="a" * 64,
        definition=None,  # type: ignore[arg-type]
        action_execution_id=uuid.uuid7(),
    )

    again = gate.request(
        write(),
        request_hash="a" * 64,
        definition=None,  # type: ignore[arg-type]
        action_execution_id=uuid.uuid7(),
    )
    assert again == approval_id
    assert gate.is_approved(approval_id, request_hash="a" * 64) is False
    with pytest.raises(NeverApprovedError):
        gate.executed(approval_id, execution_id=uuid.uuid4())
