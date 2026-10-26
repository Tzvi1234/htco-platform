"""`GET`/`PUT /v1/notifications/preferences` (slice 7.6): the caller's own preferences per kind,
per severity and the muted shops, with defaults filled in and the rules enforced: security kinds
locked, critical always in the inbox, only a visible shop muted."""

import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.incidents import IncidentCenter
from htco_application.access.authentication import Membership
from htco_application.incidents import IncidentService
from htco_application.notification_preferences import PreferenceSet
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_incident_routes import NOW, ORG, OTHER_SHOP, SHOP, Inbox, Reader
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.fulfillment_fakes import MemoryIncidentStore

pytestmark = pytest.mark.anyio

PATH = "/v1/notifications/preferences"


class Preferences:
    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, uuid.UUID], PreferenceSet] = {}

    def read(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> PreferenceSet:
        return self.rows.get((organization_id, user_id), PreferenceSet())

    def replace(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        preferences: PreferenceSet,
        *,
        now: datetime,  # noqa: ARG002 - the port
    ) -> None:
        self.rows[(organization_id, user_id)] = preferences


def client(
    store: Preferences | None, role: Role = Role.OWNER, shops: frozenset[uuid.UUID] = frozenset()
) -> httpx.AsyncClient:
    incidents = MemoryIncidentStore()
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships(Membership(ORG, role, shops))),
            incident_center=IncidentCenter(
                Reader(incidents), IncidentService(incidents, clock=lambda: NOW), Inbox(), store
            ),
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    )


def kinds(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k["kind"]: k for k in body["kinds"]}


def severities(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["severity"]: s for s in body["severities"]}


async def test_get__defaults_with_security_locked_and_critical_in_the_inbox() -> None:
    async with client(Preferences()) as http:
        answer = await http.get(PATH)
    body = answer.json()
    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    assert kinds(body)["incident_opened"] == {
        "kind": "incident_opened", "in_app": True, "email": True, "locked": False,
    }  # fmt: skip
    assert kinds(body)["incident_resolved"]["email"] is False  # its 6.4 default
    assert kinds(body)["sign_in_new_device"]["locked"] is True
    assert severities(body)["critical"]["in_app_locked"] is True
    assert body["muted_shops"] == []


@pytest.mark.ac("AC-7.6.1")
async def test_put__mute_a_shop_and_choose_per_kind_and_severity() -> None:
    store = Preferences()
    async with client(store) as http:
        answer = await http.put(
            PATH,
            json={
                "kinds": {"alert_low_review": {"in_app": True, "email": False}},
                "severities": {"low": {"in_app": False, "email": False}},
                "muted_shops": [str(SHOP)],
            },
        )
        again = (await http.get(PATH)).json()
    assert answer.status_code == 200
    assert answer.json() == again
    assert again["muted_shops"] == [str(SHOP)]
    assert kinds(again)["alert_low_review"]["email"] is False
    assert severities(again)["low"] == {
        "severity": "low", "in_app": False, "email": False, "in_app_locked": False,
    }  # fmt: skip
    [stored] = store.rows.values()
    assert stored.muted_shops == frozenset({SHOP})


@pytest.mark.ac("AC-7.6.1")
async def test_put__a_security_kind_cannot_be_changed() -> None:
    store = Preferences()
    async with client(store) as http:
        answer = await http.put(
            PATH, json={"kinds": {"mfa_changed": {"in_app": False, "email": False}}}
        )
    assert answer.status_code == 422
    assert answer.json()["type"].endswith("preference-refused")
    assert store.rows == {}


async def test_put__critical_cannot_leave_the_inbox_but_may_skip_email() -> None:
    async with client(Preferences()) as http:
        refused = await http.put(
            PATH, json={"severities": {"critical": {"in_app": False, "email": True}}}
        )
        allowed = await http.put(
            PATH, json={"severities": {"critical": {"in_app": True, "email": False}}}
        )
    assert refused.status_code == 422
    assert allowed.status_code == 200
    assert severities(allowed.json())["critical"]["email"] is False


async def test_put__a_shop_scoped_role_mutes_only_its_granted_shops() -> None:
    store = Preferences()
    async with client(store, Role.OPERATIONS, frozenset({SHOP})) as http:
        refused = await http.put(PATH, json={"muted_shops": [str(OTHER_SHOP)]})
        allowed = await http.put(PATH, json={"muted_shops": [str(SHOP)]})
    assert refused.status_code == 422
    assert allowed.status_code == 200


async def test_put__replaces_everything_so_a_left_out_choice_is_the_default_again() -> None:
    async with client(Preferences()) as http:
        await http.put(PATH, json={"muted_shops": [str(SHOP)]})
        cleared = (await http.put(PATH, json={})).json()
    assert cleared["muted_shops"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"kinds": {"no_such_kind": {"in_app": True, "email": True}}},
        {"severities": {"urgent": {"in_app": True, "email": True}}},
        {"muted_shops": ["not-a-uuid"]},
        {"muted_shops": [str(uuid.uuid4()) for _ in range(201)]},
    ],
)
async def test_put__invalid_bodies_are_422(body: dict[str, Any]) -> None:
    async with client(Preferences()) as http:
        answer = await http.put(PATH, json=body)
    assert answer.status_code == 422


async def test_unwired__503() -> None:
    async with client(None) as http:
        answer = await http.get(PATH)
    assert answer.status_code == 503
