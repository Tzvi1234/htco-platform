"""The connected shop's live data through the API, with an in-memory marketplace (invented data).

`GET /v1/shops/{shop_id}/overview|orders|listings|reviews|shipping`: any member who may see the
shop; buyer details only for roles that see buyer contacts; `no-store` for orders; problems for a
shop that is not there, not connected, missing a scope, or Etsy not answering. Nothing about a
buyer or a review reaches the logs.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import WebAuth
from htco_application.access.authentication import Membership
from htco_application.etsy_connect import EtsyConnect
from htco_application.marketplace import (
    MarketplaceCredentialsRejectedError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
)
from htco_application.shop_dashboard import ShopDashboard
from htco_application.shop_data_cache import ShopDataCache
from htco_domain.identity import Role
from htco_domain.integrations import IntegrationStatus
from htco_platform.crypto import Sealer
from htco_platform.jwt import JwtVerifier
from tests.api.test_etsy_connect_routes import (
    CREDENTIALS,
    CSRF,
    KEY,
    ORG,
    ORIGIN,
    PROBLEMS,
    Aal2AfterVerify,
    Clock,
    Mfa,
)
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks
from tests.etsy_fakes import (
    GOOD_CODE,
    REDIRECT_URI,
    AuditLog,
    AuthorizationServer,
    Connector,
    Directory,
    Pending,
    Pinning,
    Vault,
)
from tests.idempotency_fakes import MemoryIdempotencyStore
from tests.log_capture import capture_logs
from tests.session_fakes import Auth, Store
from tests.shop_data_fakes import BUYER, NOW, REVIEW_TEXT, TRACKING, ShopData

pytestmark = pytest.mark.anyio

ISRAEL = 180


class Memberships:
    """The caller's role, with the connected shop granted (for roles limited to granted shops)."""

    def __init__(self) -> None:
        self.role = Role.OWNER
        self.granted: frozenset[uuid.UUID] = frozenset()

    async def memberships_of(self, user: Any) -> Sequence[Membership]:  # noqa: ARG002 - fake
        return [Membership(ORG, self.role, self.granted)]


@dataclass
class World:
    clock: Clock
    vault: Vault
    source: ShopData
    memberships: Memberships
    http: httpx.AsyncClient
    dashboard: ShopDashboard
    shop_id: str = ""

    async def get(self, view: str, shop_id: str | None = None, **params: Any) -> httpx.Response:
        return await self.http.get(f"/v1/shops/{shop_id or self.shop_id}/{view}", params=params)

    def set_status(self, status: IntegrationStatus) -> None:
        shop = uuid.UUID(self.shop_id)
        self.vault.rows[shop] = replace(self.vault.rows[shop], status=status)


async def _world(*, dashboard_on: bool = True) -> AsyncIterator[World]:
    clock = Clock()
    vault, audit, source, memberships = Vault(), AuditLog(), ShopData(), Memberships()
    connect = EtsyConnect(
        authorization_server=AuthorizationServer(),
        directory=Directory(),
        pending=Pending(),
        connector=Connector(audit, vault=vault),
        audit=audit,
        redirect_uri=REDIRECT_URI,
    )
    dashboard = ShopDashboard(
        connections=vault,
        source=source,
        tokens=Pinning(),
        cache=ShopDataCache(clock=clock),
        successes=vault,
    )
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY)), memberships
            ),
            web_auth=WebAuth(
                link_sealer=Sealer(b"k" * 32),
                auth=Auth(),
                tokens=Aal2AfterVerify(),
                store=Store(),
                allowed_origins=(ORIGIN,),
                mfa=Mfa(),
            ),
            clock=clock,
            etsy=connect,
            token_vault=vault,
            dashboard=dashboard if dashboard_on else None,
            idempotency=MemoryIdempotencyStore(),
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        world = World(clock, vault, source, memberships, http, dashboard)
        assert (await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)).is_success
        await http.post("/v1/auth/mfa/enroll", json={"name": "Phone"}, headers=CSRF)
        verified = await http.post(
            "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "123456"}, headers=CSRF
        )
        assert verified.status_code == 200
        started = await http.post("/v1/shops/etsy/connect", headers=CSRF)
        state = httpx.URL(started.json()["authorization_url"]).params["state"]
        await http.get("/v1/integrations/etsy/callback", params={"code": GOOD_CODE, "state": state})
        done = await http.post("/v1/integrations/etsy/callback", headers=CSRF)
        assert done.status_code == 200, done.text
        world.shop_id = done.json()["shop"]["id"]
        memberships.granted = frozenset({uuid.UUID(world.shop_id)})
        yield world


@pytest.fixture
async def world() -> AsyncIterator[World]:
    async for created in _world():
        yield created


def problem(response: httpx.Response, slug: str, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == PROBLEMS + slug


# --- Overview ----------------------------------------------------------------------------------


async def test_overview__profile_sales_and_freshness(world: World) -> None:
    response = await world.get("overview", utc_offset_minutes=ISRAEL)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-cache"
    body = response.json()
    assert body["shop_id"] == world.shop_id
    assert body["source"] == "etsy_live"
    assert body["stale"] is False
    assert body["fetched_at"].startswith("2026-10-15T09:00")
    profile = body["profile"]
    assert profile["name"] == "FakeCraftsShop"
    assert profile["currency"] == "USD"
    assert profile["review_average"] == "4.8"
    assert profile["sold_count"] == 128
    sales = body["sales"]
    assert sales["today"] == {
        "orders": 2,
        "units": 3,
        "revenue": {"amount": "66.00", "currency": "USD"},
    }
    assert sales["last_7_days"]["orders"] == 3
    assert sales["last_30_days"]["revenue"]["amount"] == "114.00"
    assert (sales["awaiting_shipment"], sales["overdue"]) == (2, 1)
    assert len(sales["daily"]) == 30
    assert sales["daily"][-1] == {"date": "2026-10-15", "orders": 2, "revenue": "66.00"}
    assert sales["top_listings"][0]["listing_id"] == 701


@pytest.mark.parametrize("offset", ["-841", "841", "x"])
async def test_overview__bad_utc_offset__422(world: World, offset: str) -> None:
    response = await world.get("overview", utc_offset_minutes=offset)

    assert response.status_code == 422


async def test_overview__orders_unreadable__profile_with_null_sales(world: World) -> None:
    world.source.fail["list_order_receipts"] = MarketplacePermissionError("scope", status=403)

    response = await world.get("overview")

    assert response.status_code == 200
    assert response.json()["sales"] is None


# --- Orders ------------------------------------------------------------------------------------


async def test_orders__owner__buyer_tracking_counts_and_no_store(world: World) -> None:
    response = await world.get("orders")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["buyer_details"] is True
    assert body["total"] == 6
    assert body["counts"] == {
        "unpaid": 1,
        "paid": 2,
        "shipped": 2,
        "canceled": 1,
        "refunded": 0,
        "overdue": 1,
    }
    first = body["items"][0]
    assert first["order_id"] == 9001
    assert first["state"] == "paid"
    assert first["buyer"] == BUYER
    assert first["total"] == {"amount": "48.00", "currency": "USD"}
    assert first["items"][0]["title"] == "Fake mug"
    shipped = body["items"][2]
    assert shipped["shipments"][0]["tracking_code"] == TRACKING


@pytest.mark.parametrize("role", [Role.VIEWER, Role.FINANCE, Role.DEVELOPER])
async def test_orders__roles_without_buyer_contact__no_name_or_tracking(
    world: World, role: Role
) -> None:
    world.memberships.role = role

    response = await world.get("orders")

    assert response.status_code == 200, response.text
    assert response.json()["buyer_details"] is False
    assert BUYER not in response.text
    assert TRACKING not in response.text


async def test_orders__filter_and_page(world: World) -> None:
    response = await world.get("orders", state="overdue")
    paged = await world.get("orders", limit=2, offset=2)

    assert [o["order_id"] for o in response.json()["items"]] == [9002]
    assert [o["order_id"] for o in paged.json()["items"]] == [9003, 9004]
    assert paged.json()["total"] == 6


@pytest.mark.parametrize(
    "params", [{"state": "lost"}, {"limit": "0"}, {"limit": "101"}, {"offset": "-1"}]
)
async def test_orders__invalid_query__422(world: World, params: dict[str, str]) -> None:
    response = await world.get("orders", **params)

    assert response.status_code == 422


async def test_orders__no_buyer_detail_or_review_text_in_the_logs(world: World) -> None:
    with capture_logs() as logs:
        await world.get("orders")
        await world.get("reviews")

    assert BUYER not in logs.raw()
    assert TRACKING not in logs.raw()
    assert REVIEW_TEXT not in logs.raw()


# --- Listings, reviews, shipping ---------------------------------------------------------------


async def test_listings__images_views_and_sections(world: World) -> None:
    world.memberships.role = Role.VIEWER

    response = await world.get("listings")

    assert response.status_code == 200, response.text
    body = response.json()
    mug = body["items"][0]
    assert mug["title"] == "Fake mug"
    assert mug["thumbnail_url"] == "https://i.etsystatic.com/fake/701-170.jpg"
    assert mug["price"] == {"amount": "24.00", "currency": "USD"}
    assert mug["views"] == 801
    assert mug["section_title"] == "Fake mugs"
    assert body["items"][2]["image_url"] is None
    assert [s["title"] for s in body["sections"]] == ["Fake mugs", "Fake kitchen"]
    assert (body["total_count"], body["truncated"]) == (3, False)


async def test_reviews__average_distribution_and_items(world: World) -> None:
    response = await world.get("reviews")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["average"], body["count"], body["sample_size"]) == ("4.8", 31, 4)
    assert body["distribution"] == [
        {"stars": 5, "count": 2},
        {"stars": 4, "count": 1},
        {"stars": 3, "count": 0},
        {"stars": 2, "count": 1},
        {"stars": 1, "count": 0},
    ]
    assert body["items"][0]["text"] == REVIEW_TEXT


async def test_shipping__profiles_policies_partners(world: World) -> None:
    response = await world.get("shipping")

    assert response.status_code == 200, response.text
    body = response.json()
    [profile] = body["profiles"]
    assert profile["destinations"][0]["country"] == "US"
    assert profile["destinations"][0]["first_item"] == {"amount": "5.99", "currency": "USD"}
    assert body["return_policies"][0]["return_deadline_days"] == 30
    assert body["production_partners"][0]["name"] == "Fake Print Partner"


# --- Problems ----------------------------------------------------------------------------------


@pytest.mark.parametrize("view", ["overview", "orders", "listings", "reviews", "shipping"])
async def test_every_view__unknown_shop__404(world: World, view: str) -> None:
    problem(await world.get(view, shop_id=str(uuid.uuid4())), "not-found", 404)


async def test_every_view__invalid_shop_id__422(world: World) -> None:
    assert (await world.get("orders", shop_id="not-a-uuid")).status_code == 422


@pytest.mark.parametrize("view", ["overview", "orders", "listings", "reviews", "shipping"])
async def test_every_view__shop_disconnected__409_and_no_etsy_call(world: World, view: str) -> None:
    world.set_status(IntegrationStatus.DISCONNECTED)

    problem(await world.get(view), "shop-not-connected", 409)
    assert world.source.calls == []


@pytest.mark.parametrize("view", ["overview", "orders", "listings", "reviews", "shipping", "stats"])
async def test_every_view__needs_reauth__409_with_the_reason_and_no_etsy_call(
    world: World, view: str
) -> None:
    world.set_status(IntegrationStatus.NEEDS_REAUTH)

    response = await world.get(view)

    problem(response, "shop-not-connected", 409)
    assert response.json()["reason"] == "needs_reauth"
    assert world.source.calls == []


async def test_disconnected__409_says_disconnected(world: World) -> None:
    world.set_status(IntegrationStatus.DISCONNECTED)

    assert (await world.get("overview")).json()["reason"] == "disconnected"


@pytest.mark.parametrize(
    "error",
    [
        MarketplaceCredentialsRejectedError("refused", status=401, reason="invalid_token"),
        MarketplaceCredentialsRejectedError("refused", status=403, reason="api_key_rejected"),
    ],
)
async def test_etsy_refuses_the_credentials__409_needs_reauth_never_an_empty_answer(
    world: World, error: MarketplaceCredentialsRejectedError
) -> None:
    """The app key was replaced: Etsy refuses the stored tokens. The screens must say so."""
    world.source.fail["list_listing_details"] = error

    response = await world.get("listings")

    problem(response, "shop-not-connected", 409)
    assert response.json()["reason"] == "needs_reauth"


async def test_operations_without_a_grant__404(world: World) -> None:
    world.memberships.role = Role.OPERATIONS
    world.memberships.granted = frozenset()

    problem(await world.get("orders"), "not-found", 404)


async def test_etsy_down_nothing_cached__503_with_retry_after(world: World) -> None:
    world.source.fail["list_listing_details"] = MarketplaceUnavailableError("down", status=503)

    response = await world.get("listings")

    problem(response, "etsy-data-unavailable", 503)
    assert response.headers["retry-after"] == "30"


async def test_rate_limited__503_with_etsys_wait(world: World) -> None:
    world.source.fail["list_reviews"] = MarketplaceRateLimitedError("slow", retry_after_seconds=12)

    response = await world.get("reviews")

    problem(response, "etsy-data-unavailable", 503)
    assert response.headers["retry-after"] == "12"


async def test_scope_missing__409(world: World) -> None:
    world.source.fail["list_shipping_profiles"] = MarketplacePermissionError("scope", status=403)

    problem(await world.get("shipping"), "etsy-scope-missing", 409)


async def test_etsy_down_after_a_load__stale_answer_with_the_fetch_time(world: World) -> None:
    assert (await world.get("listings")).status_code == 200
    world.clock.now = NOW + timedelta(minutes=20)
    world.source.fail["list_listing_details"] = MarketplaceUnavailableError("down", status=503)

    response = await world.get("listings")

    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert response.json()["fetched_at"].startswith("2026-10-15T09:00")


async def test_dashboard_not_wired__503() -> None:
    async for world in _world(dashboard_on=False):
        problem(await world.get("overview"), "shop-data-unavailable", 503)


async def test_not_signed_in__401(world: World) -> None:
    await world.http.post("/v1/auth/sign-out", headers=CSRF)

    response = await world.get("overview")

    assert response.status_code == 401


async def test_disconnect__the_shops_cached_live_data_leaves_memory(world: World) -> None:
    """Codex review of #50: buyer names do not wait for the next request to be forgotten."""
    assert (await world.get("orders")).status_code == 200
    assert len(world.dashboard.cache) == 1

    response = await world.http.post(
        f"/v1/shop-integrations/{world.shop_id}/disconnect", headers=CSRF
    )

    assert response.status_code == 200, response.text
    assert len(world.dashboard.cache) == 0
