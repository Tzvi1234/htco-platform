"""The adapter's webhook re-reads (slice 5.3): one listing and its inventory, each a GET against
a strict mock that fails the test on any other method (the owner's live shop: read only)."""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from htco_application.marketplace import (
    InventorySnapshot,
    ListingSnapshot,
    MarketplaceNotFoundError,
    ProductStock,
)
from htco_integrations.etsy import EtsyConfig
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, error_body, fixture, ok
from tests.integrations.test_etsy_read_only import StrictEtsy

pytestmark = pytest.mark.anyio

LISTING = 1500000001


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> Harness:
        return Harness(handler, EtsyConfig())

    return factory


async def test_get_listing__one_get_with_the_listings_shop(make: Callable[..., Harness]) -> None:
    etsy = StrictEtsy(ok(fixture("listing.json")))
    harness = make(etsy)

    async with harness.client:
        listing = await harness.adapter.get_listing(SHOP_ID, LISTING)

    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/listings/{LISTING}")
    assert listing == ListingSnapshot(
        listing_id=LISTING,
        etsy_shop_id=SHOP_ID,
        state="active",
        quantity=4,
        updated_at=datetime.fromtimestamp(1792000000, UTC),
    )


async def test_get_listing_inventory__one_get__enabled_offerings_of_live_products(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(fixture("listing_inventory.json")))
    harness = make(etsy)

    async with harness.client:
        inventory = await harness.adapter.get_listing_inventory(SHOP_ID, LISTING)

    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/listings/{LISTING}/inventory")
    # The deleted product and the disabled offering do not count.
    assert inventory == InventorySnapshot(
        LISTING, (ProductStock(2500000001, 0), ProductStock(2500000002, 3))
    )
    assert inventory.total_quantity == 3


async def test_get_listing__a_deleted_listing_is_not_found(make: Callable[..., Harness]) -> None:
    harness = make(StrictEtsy(httpx.Response(404, json=error_body(404))))

    async with harness.client:
        with pytest.raises(MarketplaceNotFoundError):
            await harness.adapter.get_listing(SHOP_ID, LISTING)


@pytest.mark.parametrize("read", ["get_listing", "get_listing_inventory"])
async def test_listing_reads__an_invalid_id_is_refused_before_any_call(
    make: Callable[..., Harness], read: str
) -> None:
    harness = make(StrictEtsy())

    async with harness.client:
        with pytest.raises(ValueError, match="listing_id"):
            await getattr(harness.adapter, read)(SHOP_ID, 0)
