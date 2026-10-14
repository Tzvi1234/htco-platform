"""The shop repository against real Postgres and the local seed (slice 2.4 part 2)."""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from htco_application.shops import ShopQuery
from htco_persistence.database import Database
from htco_persistence.shops import PostgresShopReader

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DEMO = uuid.UUID("c1000000-0000-0000-0000-000000000000")
OTHER = uuid.UUID("d1000000-0000-0000-0000-000000000000")
DEMO_OWNER = uuid.UUID("c0000000-0000-0000-0000-000000000001")
DEMO_SUPPORT = uuid.UUID("c0000000-0000-0000-0000-000000000005")
SHOP_ONE = uuid.UUID("c3000000-0000-0000-0000-000000000001")
SHOP_TWO = uuid.UUID("c3000000-0000-0000-0000-000000000002")
OTHER_SHOP = uuid.UUID("d3000000-0000-0000-0000-000000000001")


def claims(user_id: uuid.UUID) -> dict[str, Any]:
    return {"sub": str(user_id), "role": "authenticated", "aal": "aal1"}


def query(**overrides: Any) -> ShopQuery:
    base: dict[str, Any] = {
        "organization_id": DEMO,
        "only_shops": None,
        "shop_id": None,
        "after": None,
        "limit": 10,
    }
    return ShopQuery(**(base | overrides))


@pytest.fixture
async def reader() -> AsyncIterator[PostgresShopReader]:
    url = os.environ.get("HTCO_TEST_DATABASE_URL")
    if not url:
        pytest.fail("HTCO_TEST_DATABASE_URL is not set; start the database with `just db-start`")
    database = Database(url, min_size=1, max_size=2)
    await database.start()
    try:
        yield PostgresShopReader(database)
    finally:
        await database.stop()


async def test_owner__every_shop_of_the_organization_in_id_order(
    reader: PostgresShopReader,
) -> None:
    found = await reader.list_shops(claims(DEMO_OWNER), query())
    assert [shop.id for shop in found] == [SHOP_ONE, SHOP_TWO]
    assert found[0].name == "Demo Shop One"
    assert found[0].organization_id == DEMO
    assert (found[0].marketplace, found[0].status) == ("etsy", "pending")


async def test_only_shops__limits_the_result(reader: PostgresShopReader) -> None:
    found = await reader.list_shops(claims(DEMO_OWNER), query(only_shops=frozenset({SHOP_TWO})))
    assert [shop.id for shop in found] == [SHOP_TWO]


async def test_after_and_limit__page_through_the_shops(reader: PostgresShopReader) -> None:
    first = await reader.list_shops(claims(DEMO_OWNER), query(limit=1))
    second = await reader.list_shops(claims(DEMO_OWNER), query(limit=1, after=first[0].id))
    assert [shop.id for shop in [*first, *second]] == [SHOP_ONE, SHOP_TWO]


async def test_rls__even_an_unfiltered_query_hides_ungranted_shops(
    reader: PostgresShopReader,
) -> None:
    """Support asks for every shop of its organization (no `only_shops`): RLS still hides one."""
    found = await reader.list_shops(claims(DEMO_SUPPORT), query())
    assert [shop.id for shop in found] == [SHOP_ONE]


async def test_another_tenants_shop__is_never_returned(reader: PostgresShopReader) -> None:
    by_id = await reader.list_shops(claims(DEMO_OWNER), query(shop_id=OTHER_SHOP))
    by_org = await reader.list_shops(claims(DEMO_OWNER), query(organization_id=OTHER))
    assert list(by_id) == []
    assert list(by_org) == []
