"""`GET /v1/me` and `GET /v1/shops` (slice 2.4 part 2, AC-2.4.2 and AC-2.4.3)."""

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_application.shops import ShopQuery, ShopSummary
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, USER_ID, FakeJwks, SigningKey, claims

pytestmark = pytest.mark.anyio

KEY = SigningKey("k1")
PROBLEMS = "https://ht-co.app/problems/"
ORG_A = uuid.UUID("a0000000-0000-0000-0000-000000000000")
ORG_B = uuid.UUID("b0000000-0000-0000-0000-000000000000")


def shop(n: int, org: uuid.UUID = ORG_A) -> ShopSummary:
    return ShopSummary(uuid.UUID(int=n), org, f"Shop {n}", "etsy", "active")


class Memberships:
    def __init__(self, *memberships: Membership) -> None:
        self.memberships = memberships

    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        assert user.user_id == USER_ID
        return self.memberships


class Shops:
    def __init__(self, *shops: ShopSummary) -> None:
        self.shops = sorted(shops, key=lambda s: s.id)
        self.claims: list[Mapping[str, Any]] = []

    async def list_shops(
        self, claims: Mapping[str, Any], query: ShopQuery
    ) -> Sequence[ShopSummary]:
        self.claims.append(claims)
        found = [
            s
            for s in self.shops
            if s.organization_id == query.organization_id
            and (query.only_shops is None or s.id in query.only_shops)
            and (query.shop_id is None or s.id == query.shop_id)
            and (query.after is None or s.id > query.after)
        ]
        return found[: query.limit]


ALL_SHOPS = Shops(*(shop(n) for n in range(1, 6)), shop(9, ORG_B))


async def client_for(*memberships: Membership, shops: Shops = ALL_SHOPS) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(authenticator=Authenticator(verifier, Memberships(*memberships)), shops=shops)
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(aal='aal2'))}"},
    )


@pytest.fixture
async def owner() -> AsyncIterator[httpx.AsyncClient]:
    async with await client_for(Membership(ORG_A, Role.OWNER, frozenset())) as client:
        yield client


@pytest.fixture
async def support() -> AsyncIterator[httpx.AsyncClient]:
    granted = frozenset({uuid.UUID(int=2), uuid.UUID(int=4)})
    async with await client_for(Membership(ORG_A, Role.SUPPORT, granted)) as client:
        yield client


async def test_me__user_session_strength_and_memberships() -> None:
    async with await client_for(
        Membership(ORG_A, Role.OWNER, frozenset()), Membership(ORG_B, Role.VIEWER, frozenset())
    ) as client:
        response = await client.get("/v1/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(USER_ID),
        "assurance": "aal2",
        "memberships": [
            {"organization_id": str(ORG_A), "role": "owner"},
            {"organization_id": str(ORG_B), "role": "viewer"},
        ],
    }


async def test_me__without_a_token__401() -> None:
    async with await client_for(Membership(ORG_A, Role.OWNER, frozenset())) as client:
        response = await client.get("/v1/me", headers={"Authorization": ""})
    assert response.status_code == 401


async def test_shops__owner_sees_every_shop_of_the_organization(
    owner: httpx.AsyncClient,
) -> None:
    response = await owner.get("/v1/shops")

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["items"]] == [f"Shop {n}" for n in range(1, 6)]
    assert body["items"][0] == {
        "id": str(uuid.UUID(int=1)),
        "organization_id": str(ORG_A),
        "name": "Shop 1",
        "marketplace": "etsy",
        "status": "active",
    }
    assert body["next_cursor"] is None


async def test_shops__queries_run_with_the_callers_claims() -> None:
    shops = Shops(shop(1))
    async with await client_for(Membership(ORG_A, Role.OWNER, frozenset()), shops=shops) as client:
        await client.get("/v1/shops")
    assert shops.claims[0]["sub"] == str(USER_ID)


async def test_shops__support_sees_only_granted_shops(support: httpx.AsyncClient) -> None:
    response = await support.get("/v1/shops")
    assert [item["name"] for item in response.json()["items"]] == ["Shop 2", "Shop 4"]


@pytest.mark.parametrize("target", [3, 9, 77], ids=["not-granted", "other-tenant", "unknown"])
async def test_shops__filter_naming_a_shop_the_user_cannot_see__404(
    support: httpx.AsyncClient, target: int
) -> None:
    """AC-2.4.2."""
    response = await support.get("/v1/shops", params={"shop_id": str(uuid.UUID(int=target))})
    assert response.status_code == 404
    assert response.json()["type"] == PROBLEMS + "not-found"


async def test_shops__owner_naming_another_tenants_shop__404(owner: httpx.AsyncClient) -> None:
    response = await owner.get("/v1/shops", params={"shop_id": str(uuid.UUID(int=9))})
    assert response.status_code == 404


async def test_shops__filter_naming_a_granted_shop__returns_it(support: httpx.AsyncClient) -> None:
    response = await support.get("/v1/shops", params={"shop_id": str(uuid.UUID(int=4))})
    assert [item["name"] for item in response.json()["items"]] == ["Shop 4"]


async def test_shops__pages_are_stable_and_do_not_overlap(owner: httpx.AsyncClient) -> None:
    """AC-2.4.3."""
    names: list[str] = []
    params: dict[str, str] = {"limit": "2"}
    for _ in range(5):
        body = (await owner.get("/v1/shops", params=params)).json()
        names.extend(item["name"] for item in body["items"])
        if body["next_cursor"] is None:
            break
        params = {"limit": "2", "cursor": body["next_cursor"]}

    assert names == [f"Shop {n}" for n in range(1, 6)]
    again = (await owner.get("/v1/shops", params={"limit": "2"})).json()
    assert [item["name"] for item in again["items"]] == ["Shop 1", "Shop 2"]


@pytest.mark.parametrize("limit", ["0", "101", "-3", "many"])
async def test_shops__limit_outside_1_to_100__422(owner: httpx.AsyncClient, limit: str) -> None:
    """AC-2.4.3."""
    response = await owner.get("/v1/shops", params={"limit": limit})
    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"


@pytest.mark.parametrize("cursor", ["not-a-cursor", "AAAA", "!!!", "x" * 300])
async def test_shops__invalid_cursor__422(owner: httpx.AsyncClient, cursor: str) -> None:
    response = await owner.get("/v1/shops", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["query", "cursor"]


async def test_shops__several_organizations_without_one_named__400() -> None:
    async with await client_for(
        Membership(ORG_A, Role.OWNER, frozenset()), Membership(ORG_B, Role.OWNER, frozenset())
    ) as client:
        unnamed = await client.get("/v1/shops")
        named = await client.get("/v1/shops", params={"organization_id": str(ORG_B)})

    assert unnamed.status_code == 400
    assert unnamed.json()["type"] == PROBLEMS + "organization-required"
    assert [item["name"] for item in named.json()["items"]] == ["Shop 9"]


async def test_shops__naming_an_organization_the_user_is_not_in__404(
    owner: httpx.AsyncClient,
) -> None:
    response = await owner.get("/v1/shops", params={"organization_id": str(ORG_B)})
    assert response.status_code == 404


async def test_shops__without_a_shop_store__503() -> None:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    memberships = Memberships(Membership(ORG_A, Role.OWNER, frozenset()))
    app = create_app(AppOptions(authenticator=Authenticator(verifier, memberships)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    ) as client:
        response = await client.get("/v1/shops")
    assert response.status_code == 503
    assert response.json()["type"] == PROBLEMS + "not-ready"
