"""User actions on automations (slice 5.10): the policy engine decides (AC-5.10.3)."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from htco_application.access.authentication import AuthContext, Membership, NoMembershipError
from htco_application.access.policy import Outcome
from htco_application.automation.durable import DurableAutomationEngine, StoredExecution
from htco_application.automation.engine import AutomationRefused
from htco_application.automation.records import (
    DefinitionRow,
    SyncKind,
    document_hash,
    plan_sync,
    registry_from_rows,
)
from htco_application.automation.requests import (
    AutomationForbiddenError,
    AutomationNotFoundError,
    UserTrigger,
    list_definitions,
    retry_as_admin,
    trigger_as_user,
)
from htco_application.execution.gateway import UnavailableGateway
from htco_application.shops import OrganizationRequiredError, ShopNotFoundError
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind
from htco_domain.automation import ExecutionState, TriggerKind
from htco_domain.identity import AssuranceLevel, Role
from tests.application.workforce_fixtures import (
    advisor_document,
    employee_document,
    tool_document,
)
from tests.automation_fakes import EffectBook, MemoryExecutionStore, engine_document

NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)
ORG = uuid.UUID("4a000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("4b000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("4a300000-0000-0000-0000-000000000001")
UNGRANTED = uuid.UUID("4a300000-0000-0000-0000-000000000002")
FOREIGN = uuid.UUID("4b300000-0000-0000-0000-000000000001")
USER = uuid.UUID("4a100000-0000-0000-0000-000000000001")


def auth(
    role: Role, *, shops: frozenset[uuid.UUID] = frozenset({SHOP}), orgs: int = 1
) -> AuthContext:
    memberships = [Membership(ORG, role, shops)]
    if orgs > 1:
        memberships.append(Membership(OTHER_ORG, Role.VIEWER, frozenset()))
    return AuthContext(
        user_id=USER,
        assurance=AssuranceLevel.AAL2,
        session_id=None,
        memberships=tuple(memberships),
    )


def world(**changes: Any) -> tuple[DurableAutomationEngine, MemoryExecutionStore]:
    store = MemoryExecutionStore(shops={SHOP: ORG, UNGRANTED: ORG, FOREIGN: OTHER_ORG})
    registry = Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[engine_document(**changes)],
        advisors=[advisor_document()],
    )
    engine = DurableAutomationEngine(
        lambda: registry,
        store,
        {"etsy-order-intake": EffectBook().handle},
        clock=lambda: NOW,
        jitter=lambda: 0.5,
        gateway=UnavailableGateway(),
    )
    return engine, store


def request(**changes: Any) -> UserTrigger:
    values: dict[str, Any] = {
        "automation_id": "etsy-order-intake",
        "idempotency_key": "key-1",
        "payload": {"receipt_id": "77"},
        "correlation_id": "req-1",
        "shop_id": SHOP,
    } | changes
    return UserTrigger(**values)


@pytest.mark.ac("AC-5.10.3")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.OPERATIONS, Role.DEVELOPER])
def test_trigger_as_user__a_role_with_the_capability__creates_one_user_action_execution(
    role: Role,
) -> None:
    engine, store = world()

    first = trigger_as_user(engine, store, auth(role), request(), now=NOW)
    again = trigger_as_user(engine, store, auth(role), request(), now=NOW)

    assert first.id == again.id
    assert (first.trigger, first.actor.kind, first.actor.id) == (
        TriggerKind.USER_ACTION,
        ActorKind.USER,
        str(USER),
    )
    assert first.idempotency_key == f"user:{ORG}:key-1"
    assert (first.organization_id, first.shop_id) == (ORG, SHOP)


@pytest.mark.ac("AC-5.10.3")
@pytest.mark.parametrize("role", [Role.FINANCE, Role.SUPPORT, Role.VIEWER])
def test_trigger_as_user__a_role_without_the_capability__403_and_nothing_written(
    role: Role,
) -> None:
    engine, store = world()

    with pytest.raises(AutomationForbiddenError) as refused:
        trigger_as_user(engine, store, auth(role), request(), now=NOW)

    assert refused.value.outcome is Outcome.DENY
    assert store.rows == {}


@pytest.mark.ac("AC-5.10.3")
@pytest.mark.parametrize("shop", [UNGRANTED, FOREIGN, uuid.UUID(int=7)])
def test_trigger_as_user__a_shop_not_granted_or_not_the_organizations__404(
    shop: uuid.UUID,
) -> None:
    engine, store = world()

    with pytest.raises(ShopNotFoundError):
        trigger_as_user(engine, store, auth(Role.OPERATIONS), request(shop_id=shop), now=NOW)

    assert store.rows == {}


def test_trigger_as_user__owner_reaches_every_shop_of_the_organization() -> None:
    engine, store = world()

    execution = trigger_as_user(
        engine, store, auth(Role.OWNER, shops=frozenset()), request(shop_id=UNGRANTED), now=NOW
    )

    assert execution.shop_id == UNGRANTED


def test_trigger_as_user__automation_without_a_user_capability__403() -> None:
    engine, store = world(user_capability=None)

    with pytest.raises(AutomationForbiddenError):
        trigger_as_user(engine, store, auth(Role.OWNER), request(), now=NOW)


def test_trigger_as_user__unknown_automation__404() -> None:
    engine, store = world()

    with pytest.raises(AutomationNotFoundError):
        trigger_as_user(
            engine, store, auth(Role.OWNER), request(automation_id="nope-flow"), now=NOW
        )


def test_trigger_as_user__user_action_not_callable__refused_by_the_engine() -> None:
    engine, store = world(callable_from=["event", "schedule"])

    with pytest.raises(AutomationRefused, match="user_action"):
        trigger_as_user(engine, store, auth(Role.OWNER), request(), now=NOW)


def test_trigger_as_user__several_organizations_without_naming_one__refused() -> None:
    engine, store = world()

    with pytest.raises(OrganizationRequiredError):
        trigger_as_user(engine, store, auth(Role.OWNER, orgs=2), request(), now=NOW)
    with pytest.raises(NoMembershipError):
        trigger_as_user(
            engine,
            store,
            auth(Role.OWNER),
            request(organization_id=uuid.UUID(int=9)),
            now=NOW,
        )


@pytest.mark.ac("AC-5.10.4")
def test_retry_as_admin__owner_and_admin_only() -> None:
    engine, store = world()
    execution = trigger_as_user(engine, store, auth(Role.OWNER), request(), now=NOW)
    dead = execution.transition(ExecutionState.RUNNING).transition(ExecutionState.FAILED)
    dead = dead.transition(ExecutionState.DEAD_LETTERED)
    store.rows[execution.id] = StoredExecution(dead, store.rows[execution.id].payload)

    with pytest.raises(AutomationForbiddenError):
        retry_as_admin(
            engine,
            auth(Role.OPERATIONS),
            organization_id=None,
            automation_id="etsy-order-intake",
            execution_id=execution.id,
            correlation_id="r",
            now=NOW,
        )
    retried = retry_as_admin(
        engine,
        auth(Role.ADMIN),
        organization_id=ORG,
        automation_id="etsy-order-intake",
        execution_id=execution.id,
        correlation_id="r",
        now=NOW,
    )

    assert retried.state is ExecutionState.RETRY_SCHEDULED
    assert len(store.audits) == 1


def test_list_definitions__the_office_roles_only() -> None:
    engine, _ = world()
    definition = engine.definition("etsy-order-intake")
    assert definition is not None

    assert list_definitions(auth(Role.FINANCE), [definition], organization_id=None, now=NOW)
    with pytest.raises(AutomationForbiddenError):
        list_definitions(auth(Role.SUPPORT), [definition], organization_id=None, now=NOW)


# --- The registry sync's plan (AC-5.10.7) ---------------------------------------------------


def row(document: dict[str, Any]) -> DefinitionRow:
    return DefinitionRow(
        document, document_hash(document), registry_commit="abc1234", synced_at=NOW
    )


@pytest.mark.ac("AC-5.10.7")
def test_plan_sync__new_changed_unchanged_and_missing() -> None:
    kept = engine_document("kept-flow")
    changed_before = engine_document("changed-flow")
    changed_after = engine_document("changed-flow", name="Renamed")
    gone = engine_document("gone-flow")
    new = engine_document("new-flow")

    plan = plan_sync(
        [kept, changed_after, new],
        [row(kept), row(changed_before), row(gone)],
        commit="def5678",
        now=NOW,
    )

    assert {(c.kind, c.automation_id) for c in plan.changes} == {
        (SyncKind.CHANGED, "changed-flow"),
        (SyncKind.ADDED, "new-flow"),
        (SyncKind.MISSING, "gone-flow"),
    }
    assert plan.unchanged == 1
    assert plan.blocked
    assert {r.registry_commit for _, r in plan.rows} == {"def5678"}


@pytest.mark.ac("AC-5.10.7")
def test_plan_sync__applied_rows__plan_nothing_the_second_time() -> None:
    documents = [engine_document("one-flow"), engine_document("two-flow")]
    first = plan_sync(documents, [], commit="abc1234", now=NOW)

    second = plan_sync(documents, [r for _, r in first.rows], commit="abc1234", now=NOW)

    assert len(first.changes) == 2
    assert second.is_empty
    assert not second.blocked


def test_plan_sync__a_new_version__is_added_beside_the_old_one() -> None:
    v1 = engine_document("one-flow")
    v2 = engine_document("one-flow", version=2)

    plan = plan_sync([v2], [row(v1)], commit="abc1234", now=NOW)

    assert [(c.kind, c.version) for c in plan.changes] == [(SyncKind.ADDED, 2)]
    assert not plan.blocked


def test_registry_from_rows__the_current_definitions_without_employees() -> None:
    registry = registry_from_rows([row(engine_document("one-flow"))])

    assert set(registry.automations) == {"one-flow"}
    assert registry.employees == {}
