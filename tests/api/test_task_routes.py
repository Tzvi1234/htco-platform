"""Task views (slice 7.1): `/v1/tasks` and `/v1/tasks/{id}`, with in-memory tasks behind the use
cases."""

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.tasks import TaskCenter
from htco_application.access.authentication import Membership
from htco_application.tasks import StoredTask, TaskService
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role
from htco_domain.tasks import TaskKind, TaskState
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.task_fakes import MemoryTasks

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000f701")
OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-00000000f702")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000f711")
OTHER_SHOP = uuid.UUID("00000000-0000-0000-0000-00000000f712")
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
SYSTEM = ActorRef(ActorKind.SYSTEM, "test")


class World:
    def __init__(self, role: Role = Role.OWNER, shops: frozenset[uuid.UUID] = frozenset({SHOP})):
        self.tasks = MemoryTasks()
        self.service = TaskService(self.tasks, clock=lambda: NOW)
        self.role, self.shops = role, shops

    async def task(
        self, key: str, *, shop: uuid.UUID | None = None, org: uuid.UUID = ORG
    ) -> StoredTask:
        stored, _ = await self.service.create(
            organization_id=org, shop_id=shop, kind=TaskKind.HUMAN_CHANNEL,
            title="Log in to Etsy again", assignee=SYSTEM, idempotency_key=key,
            correlation_id="c", actor=SYSTEM,
        )  # fmt: skip
        return stored

    def client(self, *, wired: bool = True) -> httpx.AsyncClient:
        verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
        app = create_app(
            AppOptions(
                authenticator=Authenticator(
                    verifier, Memberships(Membership(ORG, self.role, self.shops))
                ),
                task_center=TaskCenter(self.tasks) if wired else None,
                clock=lambda: NOW,
            )
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://api.test",
            headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
        )


async def test_list__the_organizations_tasks_with_their_status() -> None:
    world = World()
    await world.task("a")
    await world.task("other-org", org=OTHER_ORG)
    async with world.client() as http:
        answer = await http.get("/v1/tasks")
    assert answer.status_code == 200
    [item] = answer.json()["items"]
    assert (item["kind"], item["state"], item["status"]) == ("human_channel", "created", "active")
    assert item["assignee"] == {"kind": "system", "id": "test"}
    assert answer.headers["Cache-Control"] == "no-store"


async def test_list__a_shop_role_sees_its_granted_shops_only() -> None:
    world = World(Role.OPERATIONS, frozenset({SHOP}))
    mine = await world.task("mine", shop=SHOP)
    theirs = await world.task("theirs", shop=OTHER_SHOP)
    await world.task("org-wide")
    async with world.client() as http:
        items = (await http.get("/v1/tasks")).json()["items"]
        hidden = await http.get(f"/v1/tasks/{theirs.task.id}")
    assert [i["id"] for i in items] == [str(mine.task.id)]
    assert hidden.status_code == 404


@pytest.mark.parametrize(
    ("role", "status"),
    [(Role.OPERATIONS, 404), (Role.SUPPORT, 404), (Role.OWNER, 200), (Role.ADMIN, 200)],
)
async def test_detail__an_organization_wide_task_is_for_the_owner_and_admins_only(
    role: Role, status: int
) -> None:
    """Quill2 on #181 (mutant A1): the detail path keeps an organization-wide task from a shop
    role, as the list does; the API reads with its own role, so RLS is no backstop here."""
    world = World(role, frozenset({SHOP}))
    org_wide = await world.task("org-wide")
    async with world.client() as http:
        answer = await http.get(f"/v1/tasks/{org_wide.task.id}")
    assert answer.status_code == status


async def test_list__filters_by_state_and_pages() -> None:
    world = World()
    for key in ("a", "b", "c"):
        await world.task(key)
    queued = await world.task("q")
    await world.service.move(ORG, queued.task.id, TaskState.QUEUED, actor=SYSTEM)
    async with world.client() as http:
        by_state = (await http.get("/v1/tasks", params={"state": "queued"})).json()["items"]
        first = (await http.get("/v1/tasks", params={"limit": 2})).json()
        rest = (
            await http.get("/v1/tasks", params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()
    assert [i["id"] for i in by_state] == [str(queued.task.id)]
    assert len(first["items"]) == len(rest["items"]) == 2
    assert rest["next_cursor"] is None


async def test_list__an_unbounded_page__is_refused() -> None:
    async with World().client() as http:
        answer = await http.get("/v1/tasks", params={"limit": 201})
    assert answer.status_code == 422


async def test_detail__the_task_with_its_steps() -> None:
    world = World()
    stored = await world.task("a")
    await world.service.move(ORG, stored.task.id, TaskState.QUEUED, actor=SYSTEM)
    async with world.client() as http:
        body = (await http.get(f"/v1/tasks/{stored.task.id}")).json()
    assert [h["to_state"] for h in body["history"]] == ["created", "queued"]
    assert body["evidence"] is None


async def test_detail__another_organizations_task__is_not_found() -> None:
    world = World()
    theirs = await world.task("x", org=OTHER_ORG)
    async with world.client() as http:
        answer = await http.get(f"/v1/tasks/{theirs.task.id}")
    assert answer.status_code == 404


async def test_detail__unknown__is_not_found() -> None:
    async with World().client() as http:
        answer = await http.get(f"/v1/tasks/{uuid.uuid4()}")
    assert answer.status_code == 404


async def test_a_stranger_organization__is_forbidden() -> None:
    async with World().client() as http:
        answer = await http.get("/v1/tasks", params={"organization_id": str(OTHER_ORG)})
    assert answer.status_code == 403


async def test_without_a_token__unauthorized() -> None:
    async with World().client() as http:
        answer = await http.get("/v1/tasks", headers={"Authorization": ""})
    assert answer.status_code == 401


async def test_unwired__unavailable() -> None:
    async with World().client(wired=False) as http:
        answer = await http.get("/v1/tasks")
    assert answer.status_code == 503
