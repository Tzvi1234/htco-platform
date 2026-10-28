"""The AutoVA relay's routes (slice 6.10): `/v1/relay-items`, with the in-memory relay store."""

import uuid
from datetime import timedelta

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.idempotency import STAGED_KEY_REQUIRED
from htco_api.incidents import IncidentCenter
from htco_application.access.authentication import Membership
from htco_application.relay import LIST_LIMIT, RelayItem
from htco_domain.fulfillment import tracking_fingerprint
from htco_domain.identity import Role
from htco_domain.relay import RelayState
from htco_platform.jwt import JwtVerifier
from tests.api.test_incident_routes import NOW, ORG, SHOP, Inbox, Reader
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.fulfillment_fakes import MemoryIncidentStore
from tests.idempotency_fakes import MemoryIdempotencyStore, fresh_idempotency_key
from tests.log_capture import capture_logs
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
    *,
    aal: str = "aal2",
    idempotency: MemoryIdempotencyStore | None = None,
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
            idempotency=idempotency if idempotency is not None else MemoryIdempotencyStore(),
            clock=lambda: NOW,
        )
    )  # fmt: skip
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(aal=aal))}"},
        event_hooks={"request": [fresh_idempotency_key]},
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


async def test_list__a_page_of_exactly_200_has_no_next_cursor() -> None:
    """Local Codex on #159: the last page may be full; it still says nothing follows."""
    store = MemoryRelayStore()
    for receipt in range(1, LIST_LIMIT + 1):
        an_item(store, receipt=receipt)
    async with client(store) as http:
        page = await http.get("/v1/relay-items")
    assert len(page.json()["items"]) == LIST_LIMIT
    assert page.json()["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["%%%", "\u00e9", "AAAA", "x" * 256, "AAAAAAAAAAAAAAAAAAAAAA"])
async def test_list__a_malformed_cursor_is_a_validation_problem_never_a_500(cursor: str) -> None:
    """Local Codex on #159: like every cursor route (decode_cursor), a 422 validation problem."""
    async with client(MemoryRelayStore()) as http:
        answer = await http.get("/v1/relay-items", params={"cursor": cursor})
    assert answer.status_code == 422
    assert answer.headers["content-type"].startswith("application/problem+json")


async def test_unwired__503_relay_unavailable() -> None:
    async with client(None) as http:
        answer = await http.get("/v1/relay-items")
    assert answer.status_code == 503


# --- 6.10 PR B: the paste and the copy -------------------------------------------------------

CODE = "9400100000000000001234"


@pytest.mark.ac("AC-6.10.3")
async def test_pr_b_paste__handed_off_with_its_last_four_and_the_code_only_from_the_copy() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    with capture_logs() as logs:
        async with client(store) as http:
            pasted = await http.post(
                f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
            )
            copy = await http.get(f"/v1/relay-items/{item.id}/code")

    assert pasted.status_code == 200
    body = pasted.json()
    assert (body["state"], body["carrier"], body["last4"]) == ("handed_off", "usps", "1234")
    assert body["closed_reason"] is None
    assert CODE not in pasted.text
    assert pasted.headers["Cache-Control"] == "no-store"
    assert copy.status_code == 200
    assert copy.json() == {"code": CODE}
    assert copy.headers["Cache-Control"] == "no-store"
    assert [a.action for a in store.audits] == ["relay.tracking_pasted", "relay.code_revealed"]
    assert CODE not in str(logs.lines())


async def test_pr_b_paste__same_idempotency_key__replayed_without_the_number() -> None:
    """Architecture audit 6.1: a timed-out retry gets the first answer (not a 409
    relay-item-moved), pasted once; the kept answer holds the last four, never the number."""
    store, idempotency = MemoryRelayStore(), MemoryIdempotencyStore()
    item = an_item(store)
    paste = {"carrier": "usps", "code": CODE}
    headers = {"Idempotency-Key": "paste-1"}
    async with client(store, idempotency=idempotency) as http:
        first = await http.post(f"/v1/relay-items/{item.id}/tracking", json=paste, headers=headers)
        again = await http.post(f"/v1/relay-items/{item.id}/tracking", json=paste, headers=headers)

    assert [first.status_code, again.status_code] == [200, 200]
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.headers["Cache-Control"] == "no-store"
    assert [a.action for a in store.audits] == ["relay.tracking_pasted"]
    assert CODE not in repr(idempotency.rows)


async def test_pr_b_paste__without_an_idempotency_key__the_staged_rule() -> None:
    """Required once STAGED_KEY_REQUIRED flips (400, nothing pasted); until then it runs."""
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        http.event_hooks["request"] = []
        answer = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )

    if STAGED_KEY_REQUIRED:
        assert answer.status_code == 400
        assert answer.json()["type"].endswith("/idempotency-key-required")
        assert store.items[item.id].state is RelayState.AWAITING_TRACKING
    else:
        assert answer.status_code == 200
        assert store.items[item.id].state is RelayState.HANDED_OFF


@pytest.mark.ac("AC-6.10.4")
async def test_pr_b_paste__an_invalid_number_is_a_422_naming_the_carrier_only() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        answer = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": "12345"}
        )

    assert answer.status_code == 422
    problem = answer.json()
    assert problem["type"].endswith("tracking-invalid")
    assert "usps" in problem["detail"]
    assert "12345" not in answer.text
    assert store.items[item.id].state is RelayState.AWAITING_TRACKING


@pytest.mark.ac("AC-6.10.1")
async def test_pr_b_paste__again_or_a_number_used_already_is_a_409() -> None:
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


@pytest.mark.ac("AC-6.10.2")
async def test_pr_b_paste__a_number_already_on_etsy_closes_the_item() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    store.on_etsy.add((SHOP, 51, tracking_fingerprint(SHOP, CODE)))
    async with client(store) as http:
        pasted = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        copy = await http.get(f"/v1/relay-items/{item.id}/code")

    assert pasted.status_code == 200
    assert (pasted.json()["state"], pasted.json()["closed_reason"]) == (
        "closed",
        "tracking_on_etsy",
    )
    assert copy.status_code == 404  # nothing to copy: the number is on Etsy already


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.VIEWER, Role.DEVELOPER])
async def test_pr_b_only_an_owner_or_admin_pastes_and_copies(role: Role) -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
    handed_off = store.item(ORG, item.id)
    async with client(store, role) as http:
        pasted = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        copy = await http.get(f"/v1/relay-items/{item.id}/code")

    if role in {Role.OPERATIONS, Role.SUPPORT}:
        assert (pasted.status_code, pasted.json()["type"].rsplit("/", 1)[-1]) == (403, "forbidden")
    else:
        assert pasted.status_code in {403, 404}
    assert copy.status_code == 404
    assert CODE not in copy.text
    assert [a.action for a in store.audits] == ["relay.tracking_pasted"]
    assert store.item(ORG, item.id) == handed_off


async def test_pr_b_without_a_second_factor_403_mfa_required_and_no_code() -> None:
    store = MemoryRelayStore()
    item = an_item(store)
    async with client(store) as http:
        await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
    async with client(store, Role.OWNER, aal="aal1") as http:
        pasted = await http.post(
            f"/v1/relay-items/{item.id}/tracking", json={"carrier": "usps", "code": CODE}
        )
        copy = await http.get(f"/v1/relay-items/{item.id}/code")

    assert (pasted.status_code, pasted.json()["type"].rsplit("/", 1)[-1]) == (403, "mfa-required")
    assert copy.status_code == 404


async def test_pr_b_unknown_item__404_and_unwired__503() -> None:
    async with client(MemoryRelayStore()) as http:
        unknown = await http.get(f"/v1/relay-items/{uuid.uuid4()}/code")
    async with client(None) as http:
        unwired = await http.post(
            f"/v1/relay-items/{uuid.uuid4()}/tracking", json={"carrier": "usps", "code": CODE}
        )

    assert unknown.status_code == 404
    assert unwired.status_code == 503
