"""`GET /v1/shops/{shop_id}/quote-requests` (slice 7.16): the board as decimal strings, Owner and
Admins only, 503 without the reader. In-memory reader; all values are invented."""

import httpx
import pytest
from fastapi import FastAPI

from htco_api.app import AppOptions, create_app
from htco_api.auth import require_auth
from htco_api.quotes import QuoteBoardServices
from htco_domain.identity import Role
from tests.application.test_quote_board import NOW, SHOP, Reader, Shops, auth, row

pytestmark = pytest.mark.anyio

PATH = f"/v1/shops/{SHOP}/quote-requests"


def app(role: Role, *, services: bool = True) -> FastAPI:
    board = QuoteBoardServices(Reader([row("7.40", "2.10"), row("6.00", "0")]), Shops())
    application = create_app(AppOptions(quote_board=board if services else None, clock=lambda: NOW))
    application.dependency_overrides[require_auth] = lambda: auth(role)
    return application


async def get(application: FastAPI, path: str = PATH) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        return await client.get(path)


async def test_the_owner__gets_the_board_sorted_with_decimal_strings() -> None:
    response = await get(app(Role.OWNER))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    [product] = response.json()["items"]
    assert [q["total"] for q in product["quotes"]] == ["6.00", "9.50"]
    assert product["quotes"][1]["unit_price"] == "7.40"
    assert product["quotes"][1]["processing_days"] == {"min": 2, "max": 4}


async def test_operations__403() -> None:
    assert (await get(app(Role.OPERATIONS))).status_code == 403


async def test_another_shop__404() -> None:
    other = "/v1/shops/00000000-0000-0000-0000-000000000001/quote-requests"
    assert (await get(app(Role.OWNER), other)).status_code == 404


async def test_without_the_reader__503() -> None:
    response = await get(app(Role.OWNER, services=False))
    assert response.status_code == 503
