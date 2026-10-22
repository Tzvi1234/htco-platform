"""The adapter's dashboard reads: shop profile, listings, sections, orders, reviews and shipping.

Every read is a GET through the guarded `_request` (the owner's live shop: read only), carries the
token source of the connection it is made for, and maps Etsy's payload to HTCO's types. Buyer data
is minimised at the boundary: a receipt keeps only a display name (first name, last initial), and
neither that name, a tracking number nor review text appears in a repr or a log line.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from htco_application.marketplace import (
    ListingImage,
    ListingState,
    MarketplaceMoney,
    MarketplacePermissionError,
    MarketplaceResponseError,
    ShopSection,
)
from htco_integrations.etsy import EtsyConfig
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, fixture, ok, quota_headers
from tests.integrations.test_etsy_read_only import StrictEtsy
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

FAKE_NAME = "Fakename Zzbuyer"
FAKE_TRACKING = "FAKETRACK000111"
FAKE_EMAIL_LOCAL = "fake.buyer.zzz"
SINCE = datetime(2026, 9, 15, tzinfo=UTC)


def usd(amount: int) -> MarketplaceMoney:
    return MarketplaceMoney(amount=amount, divisor=100, currency_code="USD")


class Pinned:
    def __init__(self) -> None:
        self.asked: list[int] = []

    async def access_token(self, shop_id: int) -> str:
        self.asked.append(shop_id)
        return "pinned-token"


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> Harness:
        return Harness(handler, EtsyConfig())

    return factory


def receipts(**changes: Any) -> dict[str, Any]:
    page: dict[str, Any] = fixture("receipts_page.json")
    first = page["results"][0]
    first.update({"name": FAKE_NAME, "buyer_email": f"{FAKE_EMAIL_LOCAL}@example.test"} | changes)
    return page


# --- Shop profile ------------------------------------------------------------------------------


async def test_get_shop_profile__maps_counts_reputation_and_identity(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(
        ok(fixture("shop.json") | {"icon_url_fullxfull": "https://i.etsystatic.com/fake/icon.jpg"})
    )
    harness = make(etsy)
    pinned = Pinned()

    async with harness.client:
        profile = await harness.adapter.get_shop_profile(SHOP_ID, tokens=pinned)

    assert profile.shop_id == SHOP_ID
    assert profile.name == "FakeTestShop"
    assert profile.title == "Example test shop for HTCO fixtures"
    assert profile.currency_code == "USD"
    assert profile.icon_url == "https://i.etsystatic.com/fake/icon.jpg"
    assert profile.banner_url is None
    assert profile.active_listing_count == 12
    assert profile.sold_transaction_count == 42
    assert profile.favorer_count == 7
    assert profile.review_average == Decimal("4.8")
    assert profile.review_count == 5
    assert profile.created_at == datetime.fromtimestamp(1700000000, UTC)
    assert profile.ships_from_country == "IL"
    assert profile.languages == ("en-US",)
    assert profile.is_vacation is False
    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/shops/{SHOP_ID}")
    assert request.headers["authorization"] == "Bearer pinned-token"
    assert pinned.asked == [SHOP_ID]


async def test_get_shop_profile__no_reviews_yet__average_and_count_none(
    make: Callable[..., Harness],
) -> None:
    body = fixture("shop.json") | {"review_average": None, "review_count": None}
    harness = make(StrictEtsy(ok(body)))

    async with harness.client:
        profile = await harness.adapter.get_shop_profile(SHOP_ID)

    assert profile.review_average is None
    assert profile.review_count is None


# --- Listings and sections ---------------------------------------------------------------------


async def test_list_listing_details__active_with_images_views_and_tags(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(fixture("listings_with_images.json")))
    harness = make(etsy)

    async with harness.client:
        page = await harness.adapter.list_listing_details(SHOP_ID, limit=100, tokens=Pinned())

    assert page.total_count == 3
    mug, towel, poster = page.items
    assert mug.title == "Fake personalised test mug"
    assert mug.state is ListingState.ACTIVE
    assert mug.price == usd(2400)
    assert mug.views == 312
    assert mug.favorer_count == 41
    assert mug.tags == ("mug", "fake", "gift")
    assert mug.section_id == 11000001
    assert mug.is_personalizable is True
    assert mug.image == ListingImage(
        thumbnail_url="https://i.etsystatic.com/fake/170x135-1.jpg",
        url="https://i.etsystatic.com/fake/570xn-1.jpg",
        alt_text="Fake product photo 1",
    )
    assert towel.quantity == 0
    assert towel.has_variations is True
    assert poster.image is None
    assert poster.is_digital is True
    assert poster.section_id is None
    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/shops/{SHOP_ID}/listings")
    assert request.url.params["state"] == "active"
    assert request.url.params["includes"] == "Images"
    assert request.url.params["limit"] == "100"


async def test_list_sections__titles_and_counts(make: Callable[..., Harness]) -> None:
    etsy = StrictEtsy(ok(fixture("sections.json")))
    harness = make(etsy)

    async with harness.client:
        sections = await harness.adapter.list_sections(SHOP_ID, tokens=Pinned())

    assert sections == (
        ShopSection(section_id=11000001, title="Fake mugs", rank=1, active_listing_count=1),
        ShopSection(section_id=11000002, title="Fake kitchen", rank=2, active_listing_count=1),
    )
    assert etsy.requests[0].url.path.endswith(f"/shops/{SHOP_ID}/sections")


# --- Orders --------------------------------------------------------------------------------------


async def test_list_order_receipts__newest_created_first_since_the_window_start(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(receipts()))
    harness = make(etsy)

    async with harness.client:
        page = await harness.adapter.list_order_receipts(
            SHOP_ID, min_created=SINCE, limit=100, offset=0, tokens=Pinned()
        )

    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/shops/{SHOP_ID}/receipts")
    assert request.url.params["min_created"] == str(int(SINCE.timestamp()))
    assert request.url.params["sort_on"] == "created"
    assert request.url.params["sort_order"] == "desc"
    assert request.url.params["limit"] == "100"
    first, second = page.items
    assert first.receipt_id == 3000000001
    assert first.status == "paid"
    assert first.is_paid is True
    assert first.is_shipped is False
    assert first.grand_total == usd(2599)
    assert first.subtotal == usd(2000)
    assert first.shipping_total == usd(599)
    assert first.discount_total == usd(0)
    [line] = first.lines
    assert line.title == "Fake test mug"
    assert line.quantity == 2
    assert line.unit_price == usd(1000)
    assert line.sku == "FAKE-MUG-01"
    assert line.expected_ship_date == datetime.fromtimestamp(1760259200, UTC)
    assert second.status == "completed"
    assert second.lines[0].listing_id is None


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        (FAKE_NAME, "Fakename Z."),
        ("Fakename", "Fakename"),
        ("  Fakename   Middle   Zzbuyer ", "Fakename Z."),
        ("", None),
        (None, None),
    ],
)
async def test_list_order_receipts__buyer_name__first_name_and_last_initial_only(
    make: Callable[..., Harness], name: str | None, shown: str | None
) -> None:
    harness = make(StrictEtsy(ok(receipts(name=name))))

    async with harness.client:
        page = await harness.adapter.list_order_receipts(SHOP_ID, min_created=SINCE)

    assert page.items[0].buyer_display_name == shown


async def test_list_order_receipts__shipments_carrier_and_time__nothing_else_leaks(
    make: Callable[..., Harness],
) -> None:
    shipment = {
        "receipt_shipping_id": 1,
        "shipment_notification_timestamp": 1760100000,
        "carrier_name": "fake-post",
        "tracking_code": FAKE_TRACKING,
    }
    body = receipts(
        shipments=[shipment],
        is_shipped=True,
        first_line="FAKE-LINE-ZZZ",
        message_from_buyer="FAKE-MESSAGE-ZZZ",
    )
    harness = make(StrictEtsy(ok(body)))

    with capture_logs() as logs:
        async with harness.client:
            page = await harness.adapter.list_order_receipts(SHOP_ID, min_created=SINCE)

    receipt = page.items[0]
    [sent] = receipt.shipments
    assert sent.carrier == "fake-post"
    assert sent.tracking_code == FAKE_TRACKING
    assert sent.notified_at == datetime.fromtimestamp(1760100000, UTC)
    text = repr(page)
    for secret in (FAKE_NAME, "Fakename Z.", FAKE_TRACKING, FAKE_EMAIL_LOCAL, "FAKE-LINE-ZZZ"):
        assert secret not in text
        assert secret not in logs.raw()
    assert "FAKE-MESSAGE-ZZZ" not in text


async def test_list_order_receipts__naive_window_start__refused_before_any_call(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy()
    harness = make(etsy)

    async with harness.client:
        with pytest.raises(ValueError, match="min_created"):
            await harness.adapter.list_order_receipts(SHOP_ID, min_created=datetime(2026, 9, 1))  # noqa: DTZ001

    assert etsy.requests == []


async def test_list_order_receipts__malformed_payload__typed_error_without_values(
    make: Callable[..., Harness],
) -> None:
    body = receipts(grandtotal="FAKE-NOT-MONEY")
    harness = make(StrictEtsy(ok(body)))

    async with harness.client:
        with pytest.raises(MarketplaceResponseError) as raised:
            await harness.adapter.list_order_receipts(SHOP_ID, min_created=SINCE)

    assert FAKE_NAME not in str(raised.value)


# --- Reviews -----------------------------------------------------------------------------------


async def test_list_reviews__rating_listing_and_time__text_kept_out_of_repr_and_logs(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(fixture("reviews_page.json")))
    harness = make(etsy)

    with capture_logs() as logs:
        async with harness.client:
            page = await harness.adapter.list_reviews(SHOP_ID, limit=100, tokens=Pinned())

    assert page.total_count == 3
    first, second, third = page.items
    assert (first.listing_id, first.rating, first.language) == (7000000001, 5, "en")
    assert first.text == "Fake review text: lovely test mug."
    assert first.created_at == datetime.fromtimestamp(1760400000, UTC)
    assert second.text is None
    assert third.rating == 2
    assert "lovely" not in repr(page)
    assert "lovely" not in logs.raw()
    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/shops/{SHOP_ID}/reviews")
    assert request.url.params["limit"] == "100"


# --- Shipping, returns and production partners --------------------------------------------------


async def test_list_shipping_profiles__destinations_costs_and_days(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(fixture("shipping_profiles.json")))
    harness = make(etsy)

    async with harness.client:
        [profile] = await harness.adapter.list_shipping_profiles(SHOP_ID, tokens=Pinned())

    assert profile.title == "Fake standard shipping"
    assert profile.origin_country == "IL"
    assert profile.profile_type == "manual"
    assert profile.upgrade_count == 1
    us, eu = profile.destinations
    assert us.country_code == "US"
    assert us.region is None
    assert us.first_item_cost == usd(599)
    assert us.additional_item_cost == usd(200)
    assert (us.min_delivery_days, us.max_delivery_days) == (7, 14)
    assert eu.country_code is None
    assert eu.region == "eu"
    assert etsy.requests[0].url.path.endswith(f"/shops/{SHOP_ID}/shipping-profiles")


async def test_list_return_policies_and_partners(make: Callable[..., Harness]) -> None:
    etsy = StrictEtsy(ok(fixture("return_policies.json")), ok(fixture("production_partners.json")))
    harness = make(etsy)

    async with harness.client:
        [policy] = await harness.adapter.list_return_policies(SHOP_ID, tokens=Pinned())
        [partner] = await harness.adapter.list_production_partners(SHOP_ID, tokens=Pinned())

    assert (policy.accepts_returns, policy.accepts_exchanges) == (True, False)
    assert policy.return_deadline_days == 30
    assert (partner.name, partner.location) == ("Fake Print Partner", "Fake City, IL")
    paths = [r.url.path for r in etsy.requests]
    assert paths[0].endswith(f"/shops/{SHOP_ID}/policies/return")
    assert paths[1].endswith(f"/shops/{SHOP_ID}/production-partners")


async def test_dashboard_read__scope_missing__permission_error(
    make: Callable[..., Harness],
) -> None:
    harness = make(StrictEtsy(httpx.Response(403, json={"error": "x"}, headers=quota_headers())))

    async with harness.client:
        with pytest.raises(MarketplacePermissionError):
            await harness.adapter.list_shipping_profiles(SHOP_ID)


# --- Every dashboard read is a GET with the pinned token ----------------------------------------


@pytest.mark.ac("AC-4.5.2")
async def test_every_dashboard_read__is_a_get_with_the_connections_token(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(
        ok(fixture("shop.json")),
        ok(fixture("listings_with_images.json")),
        ok(fixture("sections.json")),
        ok(receipts()),
        ok(fixture("reviews_page.json")),
        ok(fixture("shipping_profiles.json")),
        ok(fixture("return_policies.json")),
        ok(fixture("production_partners.json")),
    )
    harness = make(etsy)
    pinned = Pinned()
    adapter = harness.adapter

    async with harness.client:
        await adapter.get_shop_profile(SHOP_ID, tokens=pinned)
        await adapter.list_listing_details(SHOP_ID, tokens=pinned)
        await adapter.list_sections(SHOP_ID, tokens=pinned)
        await adapter.list_order_receipts(SHOP_ID, min_created=SINCE, tokens=pinned)
        await adapter.list_reviews(SHOP_ID, tokens=pinned)
        await adapter.list_shipping_profiles(SHOP_ID, tokens=pinned)
        await adapter.list_return_policies(SHOP_ID, tokens=pinned)
        await adapter.list_production_partners(SHOP_ID, tokens=pinned)

    assert len(etsy.requests) == 8
    assert {r.method for r in etsy.requests} == {"GET"}
    assert {r.headers["authorization"] for r in etsy.requests} == {"Bearer pinned-token"}
    assert harness.tokens.asked_for == []
