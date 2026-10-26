"""The AutoVA relay's routes (slice 6.10): `/v1/relay-items`, with the in-memory relay store."""

import uuid
from datetime import timedelta

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.incidents import IncidentCenter
from htco_application.access.authentication import Membership
from htco_application.relay import LIST_LIMIT, RelayItem
from htco_domain.identity import Role
from htco_domain.relay import RelayState
from htco_platform.jwt import JwtVerifier
from tests.api.test_incident_routes import NOW, ORG, SHOP, Inbox, Reader
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.fulfillment_fakes import MemoryIncidentStore
from tests.relay_fakes import MemoryRelayStore

pytestmark = pytest.mark.anyio


def an_item(store: MemoryRelayStore, shop: uuid.UUID = SHOP, receipt: int = 51) -> RelayItem:
    item = RelayItem(
        id=uuid.uuid7(), organization_id=ORG, shop_id=shop, receipt_id=receipt,
        state=RelayState.AWAITING_TRACKING, paid_at=NOW - timedelta(days=3), opened_at=NOW,
        updated_at=NOW,
    )  # fmt: skip
    store.open_item(item)
    return item


def client(
    store: MemoryRelayStore | None,
    role: Role = Role.OWNER,
    shops: frozenset[uuid.UUID] = frozenset({SHOP}),
) -> httpx.AsyncClient:
    from htco_application.incidents import IncidentService  # noqa: PLC0415

    incidents = MemoryIncidentStore()
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY)),
                Memberships(Membership(ORG, role, shops)),
            ),
            incident_center=IncidentCenter(
                Reader(incidents), IncidentService(incidents, clock=lambda: NOW), Inbox(),
                relay=store,
            ),
            clock=lambda: NOW,
        )
    )  # fmt: skip
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    )


@pytest.mark.ac("AC-6.10.1")
async def test_list__the_open_items_without_any_code() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        answer = await http.get("/v1/relay-items")
    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    [row] = answer.json()["items"]
    assert (row["id"], row["receipt_id"], row["state"]) == (str(item.id), 51, "awaiting_tracking")
    assert "code" not in row


async def test_list__a_viewer_403_and_a_developer_follows_the_relay() -> None:
    """The list needs UPDATE_INCIDENTS: a viewer is refused, a developer (no buyer data) may
    follow the relay, which never carries a code or any part of one."""
    store = MemoryRelayStore()
    an_item(store)
    async with client(store, Role.VIEWER) as http:
        refused = await http.get("/v1/relay-items")
    async with client(store, Role.DEVELOPER) as http:
        listed = await http.get("/v1/relay-items")
    assert refused.status_code == 403
    assert listed.status_code == 200
    [row] = listed.json()["items"]
    assert not {"code", "carrier", "last4"} & set(row)


async def test_list__pages_past_the_first_200_with_a_cursor() -> None:
    """Local Codex on #159: more than one page of open items is reachable, oldest first."""
    store = MemoryRelayStore()
    for receipt in range(1, LIST_LIMIT + 2):
        store.open_item(
            RelayItem(
                id=uuid.uuid7(), organization_id=ORG, shop_id=SHOP, receipt_id=receipt,
                state=RelayState.AWAITING_TRACKING, paid_at=NOW - timedelta(days=3),
                opened_at=NOW + timedelta(seconds=receipt), updated_at=NOW,
            )
        )  # fmt: skip
    async with client(store) as http:
        first = await http.get("/v1/relay-items")
        second = await http.get("/v1/relay-items", params={"cursor": first.json()["next_cursor"]})
        bad = await http.get("/v1/relay-items", params={"cursor": "AAAAAAAAAAAAAAAAAAAAAA"})

    assert len(first.json()["items"]) == LIST_LIMIT
    assert [row["receipt_id"] for row in second.json()["items"]] == [LIST_LIMIT + 1]
    assert second.json()["next_cursor"] is None
    assert bad.status_code == 422


async def test_unwired__503_relay_unavailable() -> None:
    async with client(None) as http:
        answer = await http.get("/v1/relay-items")
    assert answer.status_code == 503
