"""The Action Gateway (slice 5.12, ADR-0019): one entry point for business actions, with the
in-memory store. AC-5.12.1 to AC-5.12.5; the Postgres store is in tests/integration."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import anyio
import pytest

from htco_application.access.policy import Principal
from htco_application.execution.channels import MemoryHumanTasks
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRef,
    ActionRequest,
    Channel,
)
from htco_application.execution.service import (
    EVENT_DESTINATION,
    ActionGatewayService,
    GatewayParts,
    request_hash,
)
from htco_application.workforce.files import read_registry
from htco_application.workforce.registry import Registry
from htco_domain.actions import ActionState
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.workforce import Readiness
from tests.application.workforce_fixtures import employee_document
from tests.automation_fakes import engine_document
from tests.execution_fakes import (
    REGISTRY,
    Approvals,
    Capabilities,
    FakeChannel,
    MemoryActionStore,
    Principals,
    Shops,
    real_catalog,
)
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("7a000000-0000-0000-0000-000000000000")
OTHER = uuid.UUID("7b000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("7a300000-0000-0000-0000-000000000001")
UNGRANTED = uuid.UUID("7a300000-0000-0000-0000-000000000002")
USER = uuid.UUID("7a100000-0000-0000-0000-000000000001")
DEFAULT_TASKS = MemoryHumanTasks()  # a marker: each world gets its own task store
SECRET = "12345678.FAKE-TOKEN-SHOULD-NEVER-APPEAR"  # noqa: S105 - a fake to search for


def office() -> Registry:
    """The real tools (two of them made ready), an active employee and an automation with
    their grants."""
    real = read_registry(REGISTRY).registry()
    tools = dict(real.tools)
    for name in ("etsy.receipts.read", "etsy.shop.read"):
        tools[name] = replace(tools[name], readiness=Readiness.ACTIVE)
    from htco_application.workforce.registry import (  # noqa: PLC0415
        parse_automation,
        parse_employee,
    )

    employee = parse_employee(employee_document("eitan", allowed_tools=["etsy.receipts.read"]))
    automation = parse_automation(
        engine_document("etsy-order-intake", allowed_actions=["etsy.receipts.read"])
    )
    return Registry(
        employees=MappingProxyType({"eitan": employee}),
        tools=MappingProxyType(tools),
        automations=MappingProxyType({"etsy-order-intake": automation}),
        advisors=MappingProxyType({}),
    )


class World:
    def __init__(
        self,
        role: Role = Role.OWNER,
        human: MemoryHumanTasks | None = DEFAULT_TASKS,
        **channel: Any,
    ) -> None:
        self.registry = office()
        self.human = MemoryHumanTasks() if human is DEFAULT_TASKS else human
        self.store = MemoryActionStore()
        self.approvals = Approvals()
        self.channel = FakeChannel(**channel)
        self.capabilities = Capabilities({(SHOP, "receipts_read"), (SHOP, "shop_details")})
        self.principals = Principals(
            {
                (USER, ORG): Principal(
                    organization_id=ORG,
                    role=role,
                    assurance=AssuranceLevel.AAL2,
                    reauthenticated_at=NOW,
                    granted_shops=frozenset({SHOP}),
                )
            }
        )
        self.gateway = ActionGatewayService(
            GatewayParts(
                catalog=real_catalog(self.registry),
                registry=lambda: self.registry,
                store=self.store,
                principals=self.principals,
                shops=Shops({SHOP: ORG, UNGRANTED: ORG}),
                approvals=self.approvals,
                adapters={Channel.API: self.channel},
                capabilities=self.capabilities,
                clock=lambda: NOW,
                human=self.human,
                # Writes are off unless their flag is on (W2, slice 6.6): these tests run one.
                write_flags=frozenset({"etsy.inventory.update"}),
            )
        )


def ask(
    action: str | ActionRef = "etsy.receipts.read",
    *,
    actor: ActorRef | None = None,
    key: str = "receipts:1",
    **changes: Any,
) -> ActionRequest:
    values: dict[str, Any] = {
        "action": action if isinstance(action, ActionRef) else ActionRef(action, 1),
        "actor": actor or ActorRef(ActorKind.AUTOMATION, "etsy-order-intake"),
        "organization_id": ORG,
        "shop_id": SHOP,
        "idempotency_key": key,
        "correlation_id": "corr-1",
        "input": {"limit": 25},
    } | changes
    return ActionRequest(**values)


USER_ACTOR = ActorRef(ActorKind.USER, str(USER))


@pytest.mark.ac("AC-5.12.1")
async def test_submit__twice__runs_once_and_returns_the_first_result() -> None:
    world = World(output={"receipts": [], "count": 0})

    first = await world.gateway.submit(ask())
    second = await world.gateway.submit(ask())

    assert first.outcome is ActionOutcome.SUCCEEDED
    assert second == first
    assert len(world.channel.calls) == 1
    assert first.verified  # the schema check of the output


@pytest.mark.ac("AC-5.12.1")
async def test_submit__concurrently__one_runs_the_other_is_deferred_then_gets_the_result() -> None:
    world = World(output={"receipts": []})
    results = []

    async def submit() -> None:
        results.append(await world.gateway.submit(ask()))

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(submit)
    later = await world.gateway.submit(ask())

    assert len(world.channel.calls) == 1
    assert {r.outcome for r in results} <= {ActionOutcome.SUCCEEDED, ActionOutcome.DEFERRED}
    assert later.outcome is ActionOutcome.SUCCEEDED


@pytest.mark.ac("AC-5.12.2")
@pytest.mark.parametrize(
    ("request_changes", "code"),
    [
        ({"action": ActionRef("etsy.nope.read", 1)}, "unknown_action"),
        ({"action": ActionRef("etsy.receipts.read", 2)}, "unknown_action"),
        ({"input": {"limit": 1000}}, "invalid_input"),
        ({"input": {"unexpected": True}}, "invalid_input"),
        ({"actor": ActorRef(ActorKind.ADVISOR, "jev")}, "forbidden"),
        ({"actor": ActorRef(ActorKind.SYSTEM, "scheduler")}, "forbidden"),  # R1, not R0
    ],
)
async def test_submit__refused_before_any_channel_with_the_reason_recorded(
    request_changes: dict[str, Any], code: str
) -> None:
    world = World()

    result = await world.gateway.submit(ask(**request_changes))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, code)
    assert world.channel.calls == []
    row = world.store.only()
    assert row.state is ActionState.DENIED
    assert row.steps[-1].reason


@pytest.mark.ac("AC-5.12.2")
@pytest.mark.parametrize(
    ("role", "shop", "code"),
    [
        (Role.FINANCE, SHOP, None),  # view_orders: Finance on granted shops
        (Role.OPERATIONS, UNGRANTED, "not_found"),
        (Role.OWNER, uuid.UUID(int=5), "not_found"),
    ],
)
async def test_submit__user_role_and_shop__decided_by_the_policy_engine(
    role: Role, shop: uuid.UUID, code: str | None
) -> None:
    world = World(role, output={"receipts": []})
    world.capabilities.granted.add((shop, "receipts_read"))

    result = await world.gateway.submit(ask(actor=USER_ACTOR, shop_id=shop))

    if code is None:
        assert result.outcome is ActionOutcome.SUCCEEDED
    else:
        assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, code)
        assert world.channel.calls == []


async def test_submit__a_role_without_the_capability__forbidden() -> None:
    world = World(Role.VIEWER)  # view_orders is masked for viewers, still allowed; shop.read too
    result = await world.gateway.submit(
        ask("etsy.inventory.update", actor=USER_ACTOR, input={"listing_id": 1, "products": []})
    )

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")


async def test_submit__a_user_of_another_organization__not_found() -> None:
    world = World()

    result = await world.gateway.submit(ask(actor=USER_ACTOR, organization_id=OTHER))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "not_found")


@pytest.mark.ac("AC-5.12.3")
async def test_write__without_approval__pending_and_nothing_runs_then_runs_once_approved() -> None:
    world = World(output={"listing_id": 1})
    write = ask(
        "etsy.inventory.update",
        actor=USER_ACTOR,
        key="inventory:1",
        input={"listing_id": 1, "products": []},
    )

    pending = await world.gateway.submit(write)
    approval_id = world.approvals.approve_last()
    done = await world.gateway.submit(replace(write, approval_id=approval_id))
    again = await world.gateway.submit(replace(write, approval_id=approval_id))

    assert pending.outcome is ActionOutcome.PENDING_APPROVAL
    assert done.outcome is ActionOutcome.SUCCEEDED
    assert again == done  # an approved request executes at most once
    assert done.verified  # re-read
    assert len(world.channel.calls) == 1
    assert world.store.only().approval_id == approval_id


@pytest.mark.ac("AC-5.12.3")
async def test_write__the_approval_is_checked_again_at_execution_time() -> None:
    world = World(Role.OWNER, output={"listing_id": 1})
    write = ask(
        "etsy.inventory.update",
        actor=USER_ACTOR,
        key="inventory:2",
        input={"listing_id": 1, "products": []},
    )
    await world.gateway.submit(write)
    approval_id = world.approvals.approve_last()
    # The user lost the role meanwhile.
    world.principals.by_user[(USER, ORG)] = replace(
        world.principals.by_user[(USER, ORG)], role=Role.VIEWER
    )

    result = await world.gateway.submit(replace(write, approval_id=approval_id))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")
    assert world.channel.calls == []


async def test_write__an_approval_for_another_request__is_refused() -> None:
    world = World(output={"listing_id": 1})
    write = ask(
        "etsy.inventory.update",
        actor=USER_ACTOR,
        key="inventory:3",
        input={"listing_id": 1, "products": []},
    )
    await world.gateway.submit(write)
    approval_id = world.approvals.approve_last()

    changed = replace(write, input={"listing_id": 2, "products": []}, approval_id=approval_id)
    result = await world.gateway.submit(replace(changed, idempotency_key="inventory:4"))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "approval_invalid")


async def test_write__the_owners_own_trigger__runs_without_an_approval() -> None:
    world = World(Role.OWNER, output={"listing_id": 1})

    result = await world.gateway.submit(
        ask(
            "etsy.inventory.update",
            actor=USER_ACTOR,
            key="inventory:5",
            input={"listing_id": 1, "products": []},
            owner_trigger=True,
        )
    )

    assert result.outcome is ActionOutcome.SUCCEEDED


async def test_write__owner_trigger_by_an_admin__forbidden() -> None:
    world = World(Role.ADMIN)

    result = await world.gateway.submit(
        ask(
            "etsy.inventory.update",
            actor=USER_ACTOR,
            key="inventory:6",
            input={"listing_id": 1, "products": []},
            owner_trigger=True,
        )
    )

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")


@pytest.mark.ac("AC-5.12.4")
async def test_result__one_row_an_audit_for_personal_data_and_an_event_without_secrets() -> None:
    world = World(output={"receipts": [{"note": SECRET}]})

    with capture_logs() as logs:
        result = await world.gateway.submit(ask(input={"limit": 25}))

    [step] = world.store.only().steps
    assert step.audit is not None  # R1: buyer data
    assert step.event is not None
    assert step.event.destination == EVENT_DESTINATION
    assert step.event.payload["outcome"] == "succeeded"
    assert step.event.action_execution_id == result.execution_id
    for text in (str(step.audit.after), str(step.event.payload), logs.raw()):
        assert SECRET not in text


async def test_result__an_r0_read__has_no_audit_record() -> None:
    world = World(output={"shop_id": 1, "name": "Fake Shop"})

    await world.gateway.submit(
        ask("etsy.shop.read", actor=ActorRef(ActorKind.SYSTEM, "scheduler"), input={})
    )

    [step] = world.store.only().steps
    assert step.audit is None
    assert step.event is not None


@pytest.mark.ac("AC-5.12.5")
async def test_scenario_h__an_automation_outside_its_permissions__denied() -> None:
    world = World()

    result = await world.gateway.submit(ask("etsy.shop.read", input={}))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")


@pytest.mark.ac("AC-5.12.5")
@pytest.mark.parametrize(
    ("employee", "action", "outcome"),
    [
        ("eitan", "etsy.shop.read", ActionOutcome.DENIED),  # not granted
        ("mallory", "etsy.receipts.read", ActionOutcome.DENIED),  # unknown employee
        ("eitan", "etsy.receipts.read", ActionOutcome.SUCCEEDED),
    ],
)
async def test_scenario_h__an_employee_outside_its_grants__denied(
    employee: str, action: str, outcome: ActionOutcome
) -> None:
    world = World(output={"receipts": []})

    result = await world.gateway.submit(
        ask(
            action,
            actor=ActorRef(ActorKind.EMPLOYEE, employee),
            input={} if "shop" in action else {"limit": 1},
        )
    )

    assert result.outcome is outcome


async def test_route__a_missing_shop_capability__scope_missing() -> None:
    world = World()
    world.capabilities.granted.clear()

    result = await world.gateway.submit(ask())

    # execution-engine.md §4 step 5: a missing capability is `DENIED (scope_missing)` (5.13).
    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "scope_missing")


async def test_channel_failure__failed_then_a_retry_with_the_same_key_runs_again() -> None:
    world = World(fail=ConnectionError("etsy down"), output={"receipts": []})

    failed = await world.gateway.submit(ask())
    world.channel.fail = None
    retried = await world.gateway.submit(ask())

    assert (failed.outcome, failed.error_code) == (ActionOutcome.FAILED, "ConnectionError")
    assert retried.outcome is ActionOutcome.SUCCEEDED
    assert len(world.channel.calls) == 2


async def test_failed_verification__failed() -> None:
    world = World(verified=False, output={"listing_id": 1})

    result = await world.gateway.submit(
        ask(
            "etsy.inventory.update",
            actor=USER_ACTOR,
            key="inventory:7",
            input={"listing_id": 1, "products": []},
            owner_trigger=True,
        )
    )

    assert (result.outcome, result.error_code) == (ActionOutcome.FAILED, "verification")


async def test_handed_to_human__waits() -> None:
    world = World(hand_off=True)

    result = await world.gateway.submit(ask())

    assert result.outcome is ActionOutcome.HANDED_TO_HUMAN
    assert result.outcome.waits


def test_request_hash__covers_the_input_not_the_approval() -> None:
    base = ask()

    assert request_hash(base) == request_hash(replace(base, approval_id=uuid.uuid7()))
    assert request_hash(base) != request_hash(replace(base, input={"limit": 26}))
    assert request_hash(base) != request_hash(replace(base, shop_id=UNGRANTED))
