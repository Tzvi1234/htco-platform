"""Order history periods and the shop's statistics through the API (invented data).

`GET /v1/shops/{shop_id}/orders?period=30d|90d|12m|all` and `GET /v1/shops/{shop_id}/stats`.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from htco_application.marketplace import MarketplaceUnavailableError
from htco_domain.identity import Role
from htco_domain.integrations import IntegrationStatus
from tests.api.test_shop_data_routes import ISRAEL, World, _world, problem
from tests.log_capture import capture_logs
from tests.shop_data_fakes import BUYER, NOW, TRACKING, default_receipts, receipt

pytestmark = pytest.mark.anyio


@pytest.fixture
async def world() -> AsyncIterator[World]:
    async for created in _world():
        created.source.receipts = [
            *default_receipts(),
            receipt(
                8001,
                created=NOW - timedelta(days=40),
                status="completed",
                shipped=True,
                country="DE",
            ),
            receipt(
                8002,
                created=NOW - timedelta(days=400),
                status="completed",
                shipped=True,
                country="US",
            ),
        ]
        yield created


async def test_orders__default_period_is_30_days(world: World) -> None:
    body = (await world.get("orders")).json()

    assert body["period"] == "30d"
    assert body["cap"] == 500
    assert body["total"] == 6


@pytest.mark.parametrize(
    ("period", "total", "cap"), [("90d", 7, 1000), ("12m", 7, 2000), ("all", 8, 2000)]
)
async def test_orders__period__lists_older_completed_orders(
    world: World, period: str, total: int, cap: int
) -> None:
    response = await world.get("orders", period=period)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["period"], body["total"], body["cap"]) == (period, total, cap)
    assert body["counts"]["shipped"] == total - 4
    assert body["partial"] is False


async def test_orders__all_time__window_starts_when_the_shop_opened(world: World) -> None:
    body = (await world.get("orders", period="all", state="shipped")).json()

    assert body["window_start"].startswith("2024-03-01T00:00")
    assert [o["order_id"] for o in body["items"]] == [9003, 9006, 8001, 8002]


@pytest.mark.parametrize("period", ["7d", "ALL", ""])
async def test_orders__unknown_period__422(world: World, period: str) -> None:
    assert (await world.get("orders", period=period)).status_code == 422


async def test_stats__profile_and_all_time_sales(world: World) -> None:
    response = await world.get("stats", utc_offset_minutes=ISRAEL)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-cache"
    body = response.json()
    assert body["shop_id"] == world.shop_id
    assert (body["source"], body["stale"]) == ("etsy_live", False)
    profile = body["profile"]
    assert profile["opened_at"].startswith("2024-03-01")
    assert (profile["sold_count"], profile["review_count"], profile["favorer_count"]) == (
        128,
        31,
        57,
    )
    assert profile["digital_listing_count"] == 1
    sales = body["sales"]
    assert sales["orders"] == 6
    assert sales["revenue"] == {"amount": "162.00", "currency": "USD"}
    assert sales["average_order_value"] == {"amount": "27.00", "currency": "USD"}
    assert sales["monthly"][0] == {"month": "2025-09-01", "orders": 1, "revenue": "24.00"}
    assert sales["monthly"][-1]["month"] == "2026-10-01"
    assert sales["best_month"]["month"] == "2026-10-01"
    assert sales["top_products"][0]["listing_id"] == 701
    assert sales["countries"] == [{"country": "DE", "orders": 1}, {"country": "US", "orders": 1}]
    assert sales["first_order_at"].startswith("2025-09-10")
    assert (sales["receipts_read"], sales["partial"]) == (8, False)


async def test_stats__no_sales__nulls_not_zeros_for_the_averages(world: World) -> None:
    world.source.receipts = []

    sales = (await world.get("stats")).json()["sales"]

    assert sales["orders"] == 0
    assert sales["average_order_value"] is None
    assert sales["best_month"] is None
    assert sales["monthly"] == []
    assert sales["first_order_at"] is None


@pytest.mark.parametrize("role", [Role.VIEWER, Role.FINANCE, Role.DEVELOPER])
async def test_stats__every_role_that_sees_the_shop__aggregates_without_buyers(
    world: World, role: Role
) -> None:
    world.memberships.role = role

    with capture_logs() as logs:
        response = await world.get("stats")

    assert response.status_code == 200, response.text
    assert BUYER not in response.text
    assert TRACKING not in response.text
    assert BUYER not in logs.raw()


async def test_stats__unknown_shop__404(world: World) -> None:
    problem(await world.get("stats", shop_id=str(uuid.uuid4())), "not-found", 404)


async def test_stats__disconnected__409_and_no_etsy_call(world: World) -> None:
    world.set_status(IntegrationStatus.DISCONNECTED)

    problem(await world.get("stats"), "shop-not-connected", 409)
    assert world.source.calls == []


@pytest.mark.parametrize("offset", ["-841", "841", "x"])
async def test_stats__bad_utc_offset__422(world: World, offset: str) -> None:
    assert (await world.get("stats", utc_offset_minutes=offset)).status_code == 422


async def test_stats__etsy_down_nothing_cached__503(world: World) -> None:
    world.source.fail["get_shop_profile"] = MarketplaceUnavailableError("down", status=503)

    response = await world.get("stats")

    problem(response, "etsy-data-unavailable", 503)
    assert response.headers["retry-after"] == "30"
