"""Status management (slice 5.18): one event per status change of an execution, an action and an
environment (AC-5.18.2), and the chain view of one order's execution in causal order
(AC-5.18.1)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.approvals import ApprovalResumer
from htco_application.automation.durable import StateChange
from htco_application.browser_environments import EnvironmentRegistry, MemoryEnvironmentStore
from htco_application.execution.gateway import ActionOutcome
from htco_application.kernel import event_payload
from htco_application.marketplace import MarketplaceRateLimitedError
from htco_application.status import (
    EXECUTION_STATE_CHANGED,
    ActionRecord,
    ChainEntryKind,
    ChainRecords,
    ChangeRecord,
    EventRecord,
    ExecutionNotVisibleError,
    ExecutionRecord,
    TriggerRecord,
    build_chain,
    execution_chain,
    execution_state_event,
)
from htco_domain.actions import ActionState
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import ExecutionState
from htco_domain.browser import ConsentRecord, EnvironmentStatus
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.status import StatusKind
from tests.application import test_action_gateway as gateway_world
from tests.application.test_approvals import Automations, write
from tests.application.test_approvals import World as ApprovalWorld
from tests.execution_fakes import MemoryActionStore

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 24, 11, 0, tzinfo=UTC)
ORG = uuid.UUID("9a000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("9a300000-0000-0000-0000-000000000001")
OTHER_SHOP = uuid.UUID("9a300000-0000-0000-0000-000000000002")
USER = uuid.UUID("9a100000-0000-0000-0000-000000000001")


def assert_one_event_per_change(store: MemoryActionStore) -> int:
    """Every recorded step that changed the status has exactly one event; a step that did not
    change it (a resubmission while waiting) has none."""
    changes = 0
    for row in store.rows.values():
        for step in row.steps:
            changed = len(step.path) > 1
            assert (step.event is not None) is changed, step.path
            changes += changed
    return changes


# --- AC-5.18.2 -----------------------------------------------------------------------------------


@pytest.mark.ac("AC-5.18.2")
async def test_execution__every_state_change_emits_exactly_one_event() -> None:
    automations = Automations()  # an automation whose write waits for an approval
    execution_id = await automations.waiting()
    automations.world.approve()
    await automations.deliver()  # the decision's event reaches the resumer (AUD-03)
    await automations.engine.execute(execution_id, worker=ActorRef(ActorKind.SYSTEM, "w"))

    store = automations.store
    assert len(store.events) == len(store.changes) >= 5
    assert [event_payload(e)["to_state"] for e in store.events] == [
        c.to_state.value for c in store.changes
    ]
    assert {e.payload["event_type"] for e in store.events} == {EXECUTION_STATE_CHANGED}
    assert len({e.idempotency_key for e in store.events}) == len(store.events)
    assert event_payload(store.events[-1])["status"] == StatusKind.SUCCEEDED.value


@pytest.mark.ac("AC-5.18.2")
def test_execution_state_event__a_replayed_change__keeps_its_key_and_event_id() -> None:
    """Codex (local) on #174, P1: the event is keyed by the recorded change's own ID, so a
    change written again (a replayed transaction or job) gives the same outbox key and
    event_id, and the outbox keeps one row; two changes with the same states at the same
    instant (a retry) stay two events."""
    change = StateChange(
        execution_id=uuid.uuid7(), organization_id=ORG, from_state=ExecutionState.FAILED,
        to_state=ExecutionState.RETRY_SCHEDULED, actor=ActorRef(ActorKind.SYSTEM, "w"),
        correlation_id="c-1", at=NOW,
    )  # fmt: skip

    first, replayed = execution_state_event(change), execution_state_event(change)
    twin = execution_state_event(replace(change, id=uuid.uuid7()))

    assert (
        first.idempotency_key
        == replayed.idempotency_key
        == (f"{EXECUTION_STATE_CHANGED}:{change.id}")
    )
    assert first.payload["event_id"] == replayed.payload["event_id"]
    assert twin.idempotency_key != first.idempotency_key
    assert twin.payload["event_id"] != first.payload["event_id"]


@pytest.mark.ac("AC-5.18.2")
async def test_execution__rejected__the_cancellation_has_its_event() -> None:
    automations = Automations()
    await automations.waiting()
    approval = automations.world.approvals.status(automations.world.only_approval())
    from htco_domain.approvals import Decision  # noqa: PLC0415

    automations.world.approvals.decide(
        approval.id, Decision.REJECT, by=automations_owner(),
        shown_hash=approval.request.request_hash,
    )  # fmt: skip
    await automations.deliver()

    assert [event_payload(e)["to_state"] for e in automations.store.events][-1] == "cancelled"
    assert len(automations.store.events) == len(automations.store.changes)
    assert isinstance(ApprovalResumer, type)


def automations_owner() -> AuthContext:
    from tests.application.test_approvals import person  # noqa: PLC0415

    return person()


@pytest.mark.ac("AC-5.18.2")
async def test_action__every_status_change_emits_exactly_one_event() -> None:
    world = ApprovalWorld()
    await world.gateway.submit(write())  # waiting for the approval
    await world.gateway.submit(write())  # still waiting: no change, no event
    world.approve()
    await world.gateway.submit(replace(write(), approval_id=world.only_approval()))

    assert assert_one_event_per_change(world.store) == 2
    types = [s.event.payload["event_type"] for s in world.store.only().steps if s.event]
    assert types == ["actions.approval_required.v1", "actions.executed.v1"]


@pytest.mark.ac("AC-5.18.2")
async def test_action__a_hand_off_and_a_deferral_emit_their_events() -> None:
    handed = gateway_world.World(hand_off=True)
    deferred = gateway_world.World(fail=MarketplaceRateLimitedError("429", retry_after_seconds=60))

    await handed.gateway.submit(gateway_world.ask())
    await handed.gateway.submit(gateway_world.ask())  # still with the person: no change
    await deferred.gateway.submit(gateway_world.ask())

    assert assert_one_event_per_change(handed.store) == 1
    assert assert_one_event_per_change(deferred.store) == 1
    [hand_off] = [s.event for s in handed.store.only().steps if s.event]
    [deferral] = [s.event for s in deferred.store.only().steps if s.event]
    assert hand_off.payload["event_type"] == "actions.handed_to_human.v1"
    assert deferral.payload["event_type"] == "actions.deferred.v1"
    assert deferral.payload["outcome"] == ActionOutcome.DEFERRED.value


@pytest.mark.ac("AC-5.18.2")
def test_environment__every_status_change_emits_exactly_one_event() -> None:
    store = MemoryEnvironmentStore()
    now = [NOW]

    def clock() -> datetime:
        now[0] += timedelta(seconds=1)
        return now[0]

    registry = EnvironmentRegistry(store, type("S", (), {"organization_of_shop":
                                   staticmethod(lambda _: ORG)})(), clock,
                                   creation_enabled=True)  # fmt: skip
    by = ActorRef(ActorKind.USER, str(USER))
    environment = registry.create(
        SHOP, by=by, consent=ConsentRecord("v1", USER, NOW, "f" * 64), correlation_id="c"
    )
    current = EnvironmentStatus.PROVISIONING
    for target in (EnvironmentStatus.READY, EnvironmentStatus.DEGRADED, EnvironmentStatus.READY,
                   EnvironmentStatus.SUSPENDED, EnvironmentStatus.NOT_CONFIGURED):  # fmt: skip
        registry.transition(environment.id, target, reason="r", by=by, expected=current,
                            correlation_id="c")  # fmt: skip
        current = target

    assert len(store.events) == len(store.changes) == 6
    assert len({e.idempotency_key for e in store.events}) == 6


# --- AC-5.18.1: the chain view -------------------------------------------------------------------

T0 = NOW


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def order_records(
    *, shop: uuid.UUID | None = SHOP, output: dict[str, Any] | None = None
) -> ChainRecords:
    """One paid order: the webhook, the intake execution, two actions, their events."""
    execution_id, read_id, write_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    approval_id = uuid.uuid4()
    x = ExecutionState
    return ChainRecords(
        execution=ExecutionRecord(execution_id, ORG, shop, "etsy-order-intake", x.SUCCEEDED,
                                  "corr-order", at(1), output=output),
        trigger=TriggerRecord("inbox-1", "event", at(0), "etsy:receipt.paid"),
        changes=[
            ChangeRecord(None, x.PENDING, "system:etsy-webhook", None, at(1)),
            ChangeRecord(x.PENDING, x.RUNNING, "system:agent-worker", None, at(2)),
            ChangeRecord(x.RUNNING, x.SUCCEEDED, "system:agent-worker", None, at(6)),
        ],
        actions=[
            ActionRecord(write_id, "etsy.inventory.update", ActionState.SUCCEEDED, "succeeded",
                         "api", None, approval_id, at(4), at(5)),
            ActionRecord(read_id, "etsy.receipts.read", ActionState.SUCCEEDED, "succeeded",
                         "api", None, None, at(3), at(3)),
        ],
        events=[
            EventRecord(uuid.uuid4(), "actions.executed.v1", write_id, at(5)),
            EventRecord(uuid.uuid4(), "actions.executed.v1", read_id, at(3)),
            EventRecord(uuid.uuid4(), EXECUTION_STATE_CHANGED, None, at(6)),
        ],
    )  # fmt: skip


@pytest.mark.ac("AC-5.18.1")
def test_chain__the_webhook_execution_actions_and_events_in_causal_order() -> None:
    chain = build_chain(order_records())

    kinds = [(e.kind, e.name) for e in chain.entries]
    k = ChainEntryKind
    assert kinds == [
        (k.TRIGGER, "etsy:receipt.paid"),
        (k.EXECUTION, "etsy-order-intake"),
        (k.EXECUTION, "etsy-order-intake"),
        (k.ACTION, "etsy.receipts.read"),
        (k.EVENT, "actions.executed.v1"),
        (k.ACTION, "etsy.inventory.update"),
        (k.APPROVAL, "etsy.inventory.update"),
        (k.EVENT, "actions.executed.v1"),
        (k.EXECUTION, "etsy-order-intake"),
        (k.EVENT, EXECUTION_STATE_CHANGED),
    ]
    actions = [e for e in chain.entries if e.kind is k.ACTION]
    assert all(e.detail["channel"] == "api" and e.detail["outcome"] == "succeeded" for e in actions)
    assert chain.status is StatusKind.SUCCEEDED


def test_chain__a_hand_off_shows_as_a_human_task() -> None:
    records = order_records()
    handed = replace(records.actions[0], state=ActionState.HANDED_TO_HUMAN, outcome=None)
    chain = build_chain(replace(records, actions=[handed]))

    [task] = [e for e in chain.entries if e.kind is ChainEntryKind.HUMAN_TASK]
    assert (task.state, task.status) == ("waiting_for_human", StatusKind.WAITING)


class Reader:
    def __init__(self, records: ChainRecords | None) -> None:
        self._records = records

    def records(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,  # noqa: ARG002
    ) -> ChainRecords | None:
        assert organization_id == ORG
        return self._records


def caller(role: Role, shops: frozenset[uuid.UUID] = frozenset({SHOP})) -> AuthContext:
    return AuthContext(
        user_id=USER, assurance=AssuranceLevel.AAL1, session_id=None,
        memberships=(Membership(ORG, role, shops),),
    )  # fmt: skip


@pytest.mark.ac("AC-5.18.1")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.OPERATIONS])
def test_execution_chain__the_office_reads_it(role: Role) -> None:
    chain = execution_chain(caller(role), Reader(order_records()), uuid.uuid4(),
                            organization_id=None, now=NOW)  # fmt: skip

    assert chain.entries[0].kind is ChainEntryKind.TRIGGER


@pytest.mark.parametrize(
    ("auth", "records", "error"),
    [
        (caller(Role.SUPPORT), order_records(), PermissionError),
        (
            caller(Role.OPERATIONS, frozenset({OTHER_SHOP})),
            order_records(),
            ExecutionNotVisibleError,
        ),
        (caller(Role.OPERATIONS), order_records(shop=None), ExecutionNotVisibleError),
        (caller(Role.OWNER), None, ExecutionNotVisibleError),
    ],
)  # fmt: skip
def test_execution_chain__refusals(
    auth: AuthContext, records: ChainRecords | None, error: type[Exception]
) -> None:
    with pytest.raises(error):
        execution_chain(auth, Reader(records), uuid.uuid4(), organization_id=None, now=NOW)


@pytest.mark.parametrize(
    ("role", "shown"), [(Role.OWNER, True), (Role.ADMIN, True), (Role.OPERATIONS, False)]
)
def test_execution_chain__the_run_output_is_for_the_owner_and_admins(
    role: Role, *, shown: bool
) -> None:
    """Per-run counts (#127): the Owner and Admins see them; a shop-scoped role does not."""
    records = order_records(output={"receipts": 2, "recorded": 1})

    chain = execution_chain(caller(role), Reader(records), uuid.uuid4(),
                            organization_id=None, now=NOW)  # fmt: skip

    assert chain.execution.output == ({"receipts": 2, "recorded": 1} if shown else None)
