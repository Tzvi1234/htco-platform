"""The incident center and the inbox (slices 6.4, 6.5): `/v1/incidents` and
`/v1/notifications`, with in-memory stores behind the use cases."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.incidents import IncidentCenter
from htco_application.access.authentication import Membership
from htco_application.incident_center import IncidentDetail, IncidentHistoryEntry
from htco_application.incidents import Detection, IncidentRecord, IncidentService, order_resource
from htco_application.notifications import Notification
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role
from htco_domain.incidents import Cause, IncidentState, ResourceKind
from htco_domain.notifications import NotificationKind
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.fulfillment_fakes import MemoryIncidentStore

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000f001")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000f002")
OTHER_SHOP = uuid.UUID("00000000-0000-0000-0000-00000000f003")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
DETECTOR = ActorRef(ActorKind.AUTOMATION, "incident-detection-escalation")


class Reader:
    def __init__(self, store: MemoryIncidentStore) -> None:
        self.store = store

    def list_incidents(
        self, organization_id: uuid.UUID, *, open_only: bool, limit: int
    ) -> Sequence[IncidentRecord]:
        rows = [i for i in self.store.incidents.values() if i.organization_id == organization_id]
        return [i for i in rows if i.state.is_open or not open_only][:limit]

    def incident_detail(
        self, organization_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentDetail | None:
        incident = self.store.get_incident(organization_id, incident_id)
        if incident is None:
            return None
        history = tuple(
            IncidentHistoryEntry(s.from_state, s.to_state, s.actor.kind.value, s.actor.id, s.at,
                                 s.note, s.evidence)
            for s in self.store.steps if s.incident_id == incident_id
        )  # fmt: skip
        return IncidentDetail(incident, history)


class Inbox:
    def __init__(self) -> None:
        self.rows: list[Notification] = []
        self.read: list[Sequence[uuid.UUID] | None] = []

    def inbox(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool, limit: int
    ) -> tuple[Sequence[Notification], int]:
        mine = [
            n for n in self.rows if n.user_id == user_id and n.organization_id == organization_id
        ]
        unread = [n for n in mine if n.read_at is None]
        return (unread if unread_only else mine)[:limit], len(unread)

    def mark_read(
        self,
        organization_id: uuid.UUID,  # noqa: ARG002
        user_id: uuid.UUID,  # noqa: ARG002
        notification_ids: Sequence[uuid.UUID] | None,
        *,
        now: datetime,  # noqa: ARG002
    ) -> int:
        self.read.append(notification_ids)
        return 1


class World:
    def __init__(self, role: Role = Role.OWNER, shops: frozenset[uuid.UUID] = frozenset({SHOP})):
        self.store = MemoryIncidentStore()
        self.service = IncidentService(self.store, clock=lambda: NOW)
        self.inbox = Inbox()
        self.role = role
        self.shops = shops

    async def incident(self, shop: uuid.UUID = SHOP, receipt: int = 7) -> IncidentRecord:
        incident, _ = await self.service.open(
            Detection(ORG, Cause.TRACKING_MISSING, ResourceKind.ORDER, order_resource(shop, receipt),
                      NOW, "sla_inspector", shop_id=shop, receipt_id=receipt),
            actor=DETECTOR,
        )  # fmt: skip
        return incident

    def client(self, *, wired: bool = True) -> httpx.AsyncClient:
        verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
        app = create_app(
            AppOptions(
                authenticator=Authenticator(
                    verifier, Memberships(Membership(ORG, self.role, self.shops))
                ),
                incident_center=IncidentCenter(Reader(self.store), self.service, self.inbox)
                if wired
                else None,
                clock=lambda: NOW,
            )
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://api.test",
            headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
        )


async def test_list__open_incidents_with_their_state_and_receipt() -> None:
    world = World()
    await world.incident()
    async with world.client() as http:
        answer = await http.get("/v1/incidents")
    assert answer.status_code == 200
    [item] = answer.json()["items"]
    assert (item["state"], item["receipt_id"], item["cause"], item["is_open"]) == (
        "detected", 7, "tracking_missing", True,
    )  # fmt: skip
    assert answer.headers["Cache-Control"] == "no-store"


async def test_list__a_shop_scoped_role_sees_its_granted_shops_only() -> None:
    world = World(Role.OPERATIONS, frozenset({SHOP}))
    await world.incident(SHOP, 7)
    await world.incident(OTHER_SHOP, 8)
    async with world.client() as http:
        items = (await http.get("/v1/incidents")).json()["items"]
        hidden = next(i for i in world.store.incidents.values() if i.shop_id == OTHER_SHOP)
        missing = await http.get(f"/v1/incidents/{hidden.id}")
    assert [i["receipt_id"] for i in items] == [7]
    assert missing.status_code == 404


async def test_detail__history_and_the_steps_a_person_may_take() -> None:
    world = World()
    incident = await world.incident()
    async with world.client() as http:
        body = (await http.get(f"/v1/incidents/{incident.id}")).json()
    assert [h["to_state"] for h in body["history"]] == ["detected"]
    assert body["can_update"] is True
    assert "resolved" not in body["next_states"]
    assert set(body["next_states"]) == {"triaged", "escalated", "verifying"}


async def test_transition__a_step_along_the_machine_by_the_owner() -> None:
    world = World()
    incident = await world.incident()
    async with world.client() as http:
        answer = await http.post(
            f"/v1/incidents/{incident.id}/transitions", json={"to_state": "triaged"}
        )
    assert answer.status_code == 200
    assert answer.json()["state"] == "triaged"
    assert world.store.steps[-1].actor.kind is ActorKind.USER


@pytest.mark.parametrize(
    ("to_state", "problem"),
    [("resolved", "evidence-required"), ("approval_required", "illegal-incident-step")],
)
async def test_transition__refusals_have_their_own_problem(to_state: str, problem: str) -> None:
    world = World()
    incident = await world.incident()
    async with world.client() as http:
        answer = await http.post(
            f"/v1/incidents/{incident.id}/transitions", json={"to_state": to_state}
        )
    assert answer.status_code == 409
    assert answer.json()["type"].endswith(problem)
    assert world.store.incidents[incident.id].state is IncidentState.DETECTED


async def test_transition__a_viewer_may_not_update() -> None:
    world = World(Role.VIEWER)
    incident = await world.incident()
    async with world.client() as http:
        detail = (await http.get(f"/v1/incidents/{incident.id}")).json()
        answer = await http.post(
            f"/v1/incidents/{incident.id}/transitions", json={"to_state": "triaged"}
        )
    assert detail["can_update"] is False
    assert detail["next_states"] == []
    assert answer.status_code == 403


async def test_unwired__503() -> None:
    async with World().client(wired=False) as http:
        answer = await http.get("/v1/incidents")
    assert answer.status_code == 503


async def test_inbox__the_callers_own_notifications_and_unread_count() -> None:
    world = World()
    me = uuid.UUID(claims()["sub"])
    world.inbox.rows = [
        Notification(uuid.uuid7(), ORG, me, NotificationKind.INCIDENT_OPENED,
                     {"receipt_id": "7"}, "k1", NOW),
        Notification(uuid.uuid7(), ORG, uuid.uuid4(), NotificationKind.INCIDENT_OPENED,
                     {"receipt_id": "8"}, "k2", NOW),
    ]  # fmt: skip
    async with world.client() as http:
        body = (await http.get("/v1/notifications")).json()
        marked = await http.post("/v1/notifications/read", json={})
    assert body["unread_count"] == 1
    assert [n["params"]["receipt_id"] for n in body["items"]] == ["7"]
    assert marked.status_code == 204
    assert world.inbox.read == [None]
