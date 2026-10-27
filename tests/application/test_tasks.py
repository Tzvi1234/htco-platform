"""Tasks (slice 7.1): idempotent creation, recorded transitions with events, completion on
evidence, and reading by role."""

import uuid
from datetime import UTC, datetime
from typing import override

import pytest

from htco_application.access.authentication import AuthContext, Membership, NoMembershipError
from htco_application.kernel import event_payload
from htco_application.tasks import (
    TASK_CREATED,
    TASK_STATE_CHANGED,
    StaleTask,
    StoredTask,
    TaskDetail,
    TaskNotVisibleError,
    TaskService,
    list_tasks,
    task_detail,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.state_machine import IllegalTransition
from htco_domain.tasks import EvidenceKind, MissingEvidenceError, TaskEvidence, TaskKind, TaskState
from tests.task_fakes import MemoryTasks

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 26, 22, 0, tzinfo=UTC)
ORG = uuid.UUID("07100000-0000-7000-8000-0000000000aa")
OTHER_ORG = uuid.UUID("07100000-0000-7000-8000-0000000000bb")
SHOP = uuid.UUID("07100000-0000-7000-8000-000000000001")
OTHER_SHOP = uuid.UUID("07100000-0000-7000-8000-000000000002")
OWNER = uuid.UUID("07100000-0000-7000-8000-0000000000e1")
SYSTEM = ActorRef(ActorKind.SYSTEM, "test")
EVIDENCE = TaskEvidence(EvidenceKind.EXECUTION, "0190a000-0000-7000-8000-0000000000ee")


def member(role: Role = Role.OWNER, shops: frozenset[uuid.UUID] = frozenset()) -> AuthContext:
    return AuthContext(
        user_id=OWNER, assurance=AssuranceLevel.AAL2, session_id=None,
        memberships=(Membership(ORG, role, shops),),
    )  # fmt: skip


def service(store: MemoryTasks | None = None) -> tuple[TaskService, MemoryTasks]:
    tasks = store or MemoryTasks()
    return TaskService(tasks, clock=lambda: NOW), tasks


async def create(svc: TaskService, key: str = "hc:1", **changes: object) -> StoredTask:
    values: dict[str, object] = {
        "organization_id": ORG, "kind": TaskKind.HUMAN_CHANNEL, "title": "Log in to Etsy again",
        "assignee": ActorRef(ActorKind.USER, str(OWNER)), "idempotency_key": key,
        "correlation_id": "corr-1", "actor": SYSTEM,
    }  # fmt: skip
    values.update(changes)
    stored, _ = await svc.create(**values)  # type: ignore[arg-type]
    return stored


@pytest.mark.ac("AC-7.1.2")
async def test_create_twice_with_one_key__returns_the_first_task_and_writes_once() -> None:
    svc, tasks = service()

    first, created = await svc.create(
        organization_id=ORG, kind=TaskKind.GENERAL, title="One", assignee=SYSTEM,
        idempotency_key="k", correlation_id="c", actor=SYSTEM,
    )  # fmt: skip
    again, created_again = await svc.create(
        organization_id=ORG, kind=TaskKind.GENERAL, title="Two", assignee=SYSTEM,
        idempotency_key="k", correlation_id="c", actor=SYSTEM,
    )  # fmt: skip

    assert (created, created_again) == (True, False)
    assert again == first
    assert len(tasks.tasks) == len(tasks.steps) == len(tasks.outbox) == 1


async def test_the_same_key_in_another_organization__is_another_task() -> None:
    svc, tasks = service()

    await create(svc)
    await create(svc, organization_id=OTHER_ORG)

    assert len(tasks.tasks) == 2


async def test_create__starts_created_with_its_event() -> None:
    svc, tasks = service()

    stored = await create(svc, shop_id=SHOP)

    assert stored.task.state is TaskState.CREATED
    [step] = tasks.steps
    assert (step.from_state, step.to_state, step.actor) == (None, TaskState.CREATED, SYSTEM)
    [event] = tasks.outbox
    assert event.payload["event_type"] == TASK_CREATED
    assert event.shop_id == SHOP
    facts = event_payload(event)
    assert facts["kind"] == "human_channel"
    assert "title" not in facts  # IDs and states only


async def test_move__records_the_step_and_its_event() -> None:
    svc, tasks = service()
    stored = await create(svc)

    moved = await svc.move(ORG, stored.task.id, TaskState.QUEUED, actor=SYSTEM)

    assert moved.task.state is TaskState.QUEUED
    assert [(s.from_state, s.to_state) for s in tasks.steps][-1] == (
        TaskState.CREATED,
        TaskState.QUEUED,
    )
    event = tasks.outbox[-1]
    assert event.payload["event_type"] == TASK_STATE_CHANGED
    assert event_payload(event)["to_state"] == "queued"


@pytest.mark.ac("AC-7.1.1")
async def test_move__an_illegal_step__is_refused_and_writes_nothing() -> None:
    svc, tasks = service()
    stored = await create(svc)

    with pytest.raises(IllegalTransition):
        await svc.move(ORG, stored.task.id, TaskState.RUNNING, actor=SYSTEM)

    assert len(tasks.steps) == len(tasks.outbox) == 1


async def running(svc: TaskService) -> StoredTask:
    stored = await create(svc)
    for state in (TaskState.QUEUED, TaskState.CLAIMED, TaskState.RUNNING):
        stored = await svc.move(ORG, stored.task.id, state, actor=SYSTEM)
    return stored


@pytest.mark.ac("AC-7.1.3")
async def test_complete__only_with_its_evidence_which_is_recorded() -> None:
    svc, tasks = service()
    stored = await running(svc)

    with pytest.raises(MissingEvidenceError):
        await svc.move(ORG, stored.task.id, TaskState.COMPLETED, actor=SYSTEM)
    done = await svc.move(ORG, stored.task.id, TaskState.COMPLETED, actor=SYSTEM, evidence=EVIDENCE)

    assert done.task.evidence == EVIDENCE
    assert tasks.steps[-1].evidence == EVIDENCE
    assert event_payload(tasks.outbox[-1])["evidence_kind"] == "execution"


async def test_move__a_task_that_moved_meanwhile__is_stale() -> None:
    svc, tasks = service()
    stored = await create(svc)
    await svc.move(ORG, stored.task.id, TaskState.QUEUED, actor=SYSTEM)

    with pytest.raises(StaleTask):
        tasks.move(stored, stored, tasks.steps[0], ())


async def test_move__another_organizations_task__does_not_exist() -> None:
    svc, _ = service()
    stored = await create(svc)

    with pytest.raises(TaskNotVisibleError):
        await svc.move(OTHER_ORG, stored.task.id, TaskState.QUEUED, actor=SYSTEM)


# --- Reading ----------------------------------------------------------------------------------


async def test_list__the_owner_sees_the_organization_newest_first() -> None:
    svc, tasks = service()
    first = await create(svc, key="a")
    second = await create(svc, key="b", shop_id=SHOP)
    await create(svc, key="c", organization_id=OTHER_ORG)

    page = list_tasks(member(), tasks, organization_id=ORG, now=NOW)

    assert {t.task.id for t in page.items} == {first.task.id, second.task.id}
    assert page.next_before is None


async def test_list__a_shop_role_sees_its_granted_shops_only() -> None:
    svc, tasks = service()
    await create(svc, key="org-wide")
    mine = await create(svc, key="mine", shop_id=SHOP)
    await create(svc, key="theirs", shop_id=OTHER_SHOP)

    page = list_tasks(
        member(Role.OPERATIONS, frozenset({SHOP})), tasks, organization_id=ORG, now=NOW
    )

    assert [t.task.id for t in page.items] == [mine.task.id]


async def test_list__pages_with_a_cursor() -> None:
    svc, tasks = service()
    for key in ("a", "b", "c"):
        await create(svc, key=key)

    first = list_tasks(member(), tasks, organization_id=ORG, now=NOW, limit=2)
    rest = list_tasks(
        member(), tasks, organization_id=ORG, now=NOW, limit=2, before=first.next_before
    )

    assert len(first.items) == 2
    assert first.next_before is not None
    assert len(rest.items) == 1
    assert rest.next_before is None


async def test_list__filters_by_state_and_kind() -> None:
    svc, tasks = service()
    hc = await create(svc, key="hc")
    await create(svc, key="g", kind=TaskKind.GENERAL)

    by_kind = list_tasks(member(), tasks, organization_id=ORG, now=NOW, kind=TaskKind.HUMAN_CHANNEL)
    by_state = list_tasks(
        member(), tasks, organization_id=ORG, now=NOW, states=frozenset({TaskState.QUEUED})
    )

    assert [t.task.id for t in by_kind.items] == [hc.task.id]
    assert by_state.items == ()


def test_list__a_stranger__has_no_membership() -> None:
    with pytest.raises(NoMembershipError):
        list_tasks(member(), MemoryTasks(), organization_id=OTHER_ORG, now=NOW)


@pytest.mark.parametrize("limit", [0, 201])
def test_list__an_unbounded_page__is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="page"):
        list_tasks(member(), MemoryTasks(), organization_id=ORG, now=NOW, limit=limit)


async def test_detail__the_task_with_its_history() -> None:
    svc, tasks = service()
    stored = await create(svc)
    await svc.move(ORG, stored.task.id, TaskState.QUEUED, actor=SYSTEM)

    detail = task_detail(member(), tasks, stored.task.id, organization_id=ORG, now=NOW)

    assert [s.to_state for s in detail.history] == [TaskState.CREATED, TaskState.QUEUED]


async def test_detail__a_stranger__is_refused_before_anything_is_read() -> None:
    """Sage-arch on #181: membership first; the store is never asked."""
    svc, _ = service()
    stored = await create(svc, organization_id=OTHER_ORG)

    class Untouchable(MemoryTasks):
        @override
        def task_detail(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> TaskDetail | None:
            raise AssertionError("the store was read for a stranger")

    with pytest.raises(TaskNotVisibleError):
        task_detail(member(), Untouchable(), stored.task.id, organization_id=OTHER_ORG, now=NOW)


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT])
async def test_detail__an_organization_wide_task__does_not_exist_for_a_shop_role(
    role: Role,
) -> None:
    """Quill2 on #181 (mutant A1): the detail hides an organization-wide task from shop roles."""
    svc, tasks = service()
    org_wide = await create(svc)

    with pytest.raises(TaskNotVisibleError):
        task_detail(member(role, frozenset({SHOP})), tasks, org_wide.task.id,
                    organization_id=ORG, now=NOW)  # fmt: skip


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_detail__an_organization_wide_task__is_the_owners_and_admins(role: Role) -> None:
    svc, tasks = service()
    org_wide = await create(svc)

    detail = task_detail(member(role), tasks, org_wide.task.id, organization_id=ORG, now=NOW)

    assert detail.stored.task.id == org_wide.task.id


async def test_detail__a_task_of_another_shop__does_not_exist_for_a_shop_role() -> None:
    svc, tasks = service()
    stored = await create(svc, shop_id=OTHER_SHOP)

    with pytest.raises(TaskNotVisibleError):
        task_detail(
            member(Role.OPERATIONS, frozenset({SHOP})), tasks, stored.task.id,
            organization_id=ORG, now=NOW,
        )  # fmt: skip
