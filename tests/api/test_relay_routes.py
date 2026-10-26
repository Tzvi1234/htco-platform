"""The AutoVA relay's routes (slice 6.10): `/v1/relay-items`, with the in-memory relay store."""

import uuid
from datetime import timedelta

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.incidents import IncidentCenter
from htco_application.access.authentication import Membership
from htco_application.relay import RelayItem
from htco_domain.identity import Role
from htco_domain.relay import RelayState
from htco_platform.jwt import JwtVerifier
from tests.api.test_incident_routes import NOW, ORG, OTHER_SHOP, SHOP, Inbox, Reader
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.fulfillment_fakes import MemoryIncidentStore
from tests.relay_fakes import MemoryRelayStore

pytestmark = pytest.mark.anyio

CODE = "9400100000000000001234"


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


@pytest.mark.ac("AC-6.10.3")
async def test_paste__handed_off_with_its_last_four_and_the_code_for_the_copy() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        pasted = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        copy = await http.get(f"/v1/relay-items/{item.id}/code")
    assert pasted.status_code == 200
    body = pasted.json()
    assert (body["state"], body["carrier"], body["last4"]) == ("handed_off", "usps", "1234")
    assert CODE not in pasted.text
    assert copy.status_code == 200
    assert copy.json() == {"code": CODE}
    assert copy.headers["Cache-Control"] == "no-store"


@pytest.mark.ac("AC-6.10.4")
async def test_paste__an_invalid_number_is_a_422_with_a_clear_message() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        answer = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": "12345"}
        )
    assert answer.status_code == 422
    problem = answer.json()
    assert problem["type"].endswith("tracking-invalid")
    assert "not a valid usps tracking number" in problem["detail"]
    assert store.items[item.id].state is RelayState.AWAITING_TRACKING


async def test_paste__again_or_a_number_used_already_is_a_409() -> None:
    store = MemoryRelayStore()
    first, second = an_item(store, receipt=51), an_item(store, receipt=52)
    async with client(store) as http:
        await http.post(
            f"/v1/relay-items/{first.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        again = await http.post(
            f"/v1/relay-items/{first.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        used = await http.post(
            f"/v1/relay-items/{second.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
    assert (again.status_code, again.json()["type"].rsplit("/", 1)[-1]) == (409, "relay-item-moved")
    assert (used.status_code, used.json()["type"].rsplit("/", 1)[-1]) == (
        409,
        "tracking-already-used",
    )


async def test_refusals__a_viewer_403_an_ungranted_shop_404_and_no_code_for_the_viewer() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    hidden = an_item(store, shop=OTHER_SHOP, receipt=61)
    async with client(store, Role.VIEWER) as http:
        listed = await http.get("/v1/relay-items")
        pasted = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        code = await http.get(f"/v1/relay-items/{item.id}/code")
    async with client(store, Role.OPERATIONS) as http:
        foreign = await http.post(
            f"/v1/relay-items/{hidden.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
    assert listed.status_code == 403
    assert pasted.status_code == 403
    assert code.status_code == 404
    assert foreign.status_code == 404
    assert store.codes == {}


async def test_refusals__a_developer_updates_incidents_but_never_sees_or_pastes_a_code() -> None:
    """Quill on #159: DEVELOPER has UPDATE_INCIDENTS on its shops but no buyer data, so it is the
    role that tells the two rights apart. The paste is a 403, the code a 404, and no reveal is
    audited."""
    store = MemoryRelayStore()
    item, pasted_by_owner = an_item(store, receipt=51), an_item(store, receipt=52)
    async with client(store) as http:
        await http.post(
            f"/v1/relay-items/{pasted_by_owner.id}/tracking",
            json={"carrier": "usps", "code": CODE},
        )
    audits_before = list(store.audits)
    async with client(store, Role.DEVELOPER) as http:
        listed = await http.get("/v1/relay-items")
        pasted = await http.post(
            f"/v1/relay-items/{item.id}/tracking",
            json={"carrier": "usps", "code": "9400100000000000005678"},
        )
        code = await http.get(f"/v1/relay-items/{pasted_by_owner.id}/code")
    assert listed.status_code == 200  # it may follow the relay, never its codes
    assert pasted.status_code == 403
    assert store.items[item.id].state is RelayState.AWAITING_TRACKING
    assert item.id not in store.codes
    assert code.status_code == 404
    assert CODE not in code.text
    assert store.audits == audits_before  # no relay.code_revealed, no paste


async def test_unwired__503_relay_unavailable() -> None:
    async with client(None) as http:
        answer = await http.get("/v1/relay-items")
    assert answer.status_code == 503
