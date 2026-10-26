"""An in-memory marketplace for the dashboard's live shop data, with invented values only.

Shared by the application and API tests. Buyer names and tracking numbers are obviously fake
markers, so leak checks can search for them.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from htco_application.kernel import Page
from htco_application.marketplace import (
    AccessTokenProvider,
    ListingDetails,
    ListingImage,
    ListingState,
    MarketplaceError,
    MarketplaceMoney,
    OrderLine,
    OrderReceipt,
    OrderShipment,
    ProductionPartner,
    ReturnPolicy,
    ShippingDestination,
    ShippingProfile,
    ShopProfile,
    ShopReview,
    ShopSection,
)

# The shared fakes' time (tests.etsy_fakes.NOW): 09:00 UTC, 12:00 in Israel (UTC+3).
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
BUYER = "Fakebuyer Q."
TRACKING = "FAKE-TRACK-0042"
REVIEW_TEXT = "Fake review words, lovely."


def usd(cents: int) -> MarketplaceMoney:
    return MarketplaceMoney(amount=cents, divisor=100, currency_code="USD")


def profile(shop_id: int) -> ShopProfile:
    return ShopProfile(
        shop_id=shop_id,
        name="FakeCraftsShop",
        title="Fake crafts for tests",
        url="https://www.etsy.com/shop/FakeCraftsShop",
        currency_code="USD",
        icon_url="https://i.etsystatic.com/fake/icon.jpg",
        banner_url=None,
        is_vacation=False,
        active_listing_count=3,
        digital_listing_count=1,
        sold_transaction_count=128,
        favorer_count=57,
        review_average=Decimal("4.8"),
        review_count=31,
        created_at=datetime(2024, 3, 1, tzinfo=UTC),
        ships_from_country="IL",
        location_country="IL",
        languages=("en-US",),
        accepts_custom_requests=True,
    )


def line(  # noqa: PLR0913 - one order line's fields
    transaction_id: int,
    *,
    listing_id: int | None = 701,
    title: str = "Fake mug",
    quantity: int = 1,
    cents: int = 2400,
    digital: bool = False,
    ship_by: datetime | None = None,
) -> OrderLine:
    return OrderLine(
        transaction_id=transaction_id,
        listing_id=listing_id,
        title=title,
        sku=f"FAKE-{transaction_id}",
        quantity=quantity,
        unit_price=usd(cents),
        is_digital=digital,
        expected_ship_date=ship_by,
    )


def receipt(  # noqa: PLR0913 - one receipt's fields
    receipt_id: int,
    *,
    created: datetime,
    status: str = "paid",
    paid: bool = True,
    shipped: bool = False,
    total_cents: int = 2400,
    lines: tuple[OrderLine, ...] | None = None,
    shipments: tuple[OrderShipment, ...] = (),
    buyer: str | None = BUYER,
    country: str | None = None,
) -> OrderReceipt:
    return OrderReceipt(
        receipt_id=receipt_id,
        status=status,
        is_paid=paid,
        is_shipped=shipped,
        created_at=created,
        updated_at=created,
        grand_total=usd(total_cents),
        subtotal=usd(total_cents),
        shipping_total=usd(0),
        discount_total=usd(0),
        lines=lines if lines is not None else (line(receipt_id * 10),),
        shipments=shipments,
        buyer_display_name=buyer,
        country_code=country,
    )


def default_receipts() -> list[OrderReceipt]:
    """Today (Israel) two sales, one late; this week one shipped; a cancelled and an unpaid one;
    one sale 20 days ago."""
    return [
        receipt(
            9001,
            created=NOW - timedelta(hours=1),
            total_cents=4800,
            lines=(line(90011, quantity=2, ship_by=NOW + timedelta(days=2)),),
        ),
        receipt(
            9002,
            created=NOW - timedelta(hours=8),
            lines=(
                line(
                    90021,
                    listing_id=702,
                    title="Fake towel",
                    cents=1800,
                    ship_by=NOW - timedelta(hours=1),
                ),
            ),
            total_cents=1800,
        ),
        receipt(
            9003,
            created=NOW - timedelta(days=3),
            status="completed",
            shipped=True,
            shipments=(
                OrderShipment(
                    carrier="fake-post", tracking_code=TRACKING, notified_at=NOW - timedelta(days=2)
                ),
            ),
        ),
        receipt(9004, created=NOW - timedelta(days=4), status="canceled", total_cents=9900),
        receipt(9005, created=NOW - timedelta(days=5), status="open", paid=False, total_cents=3000),
        receipt(9006, created=NOW - timedelta(days=20), status="completed", shipped=True),
    ]


def listing(
    listing_id: int, title: str, *, section: int | None = 11, image: bool = True
) -> ListingDetails:
    return ListingDetails(
        listing_id=listing_id,
        title=title,
        state=ListingState.ACTIVE,
        quantity=5,
        price=usd(2400),
        url=f"https://www.etsy.com/listing/{listing_id}/fake",
        image=ListingImage(
            thumbnail_url=f"https://i.etsystatic.com/fake/{listing_id}-170.jpg",
            url=f"https://i.etsystatic.com/fake/{listing_id}-570.jpg",
            alt_text=None,
        )
        if image
        else None,
        views=100 + listing_id,
        favorer_count=7,
        tags=("fake",),
        section_id=section,
        is_digital=False,
        is_personalizable=False,
        has_variations=False,
        created_at=NOW - timedelta(days=100),
        updated_at=NOW - timedelta(days=1),
    )


@dataclass
class ShopData:
    """The `ShopDataSource` port in memory. `fail[operation]` raises that error instead."""

    receipts: list[OrderReceipt] = field(default_factory=default_receipts)
    listings: list[ListingDetails] = field(
        default_factory=lambda: [
            listing(701, "Fake mug"),
            listing(702, "Fake towel", section=12),
            listing(703, "Fake poster", section=None, image=False),
        ]
    )
    reviews: list[ShopReview] = field(
        default_factory=lambda: [
            ShopReview(701, 5, "en", NOW - timedelta(days=1), text=REVIEW_TEXT),
            ShopReview(702, 4, "en", NOW - timedelta(days=2)),
            ShopReview(701, 5, "en", NOW - timedelta(days=9)),
            ShopReview(702, 2, "en", NOW - timedelta(days=12), text="Fake late parcel."),
        ]
    )
    fail: dict[str, MarketplaceError] = field(default_factory=dict)
    calls: list[tuple[str, int, object]] = field(default_factory=list)
    # The `min_created` of every receipts read (None: the whole history).
    windows: list[datetime | None] = field(default_factory=list)

    def _record(
        self, operation: str, shop_id: int, tokens: AccessTokenProvider | None, *, urgent: bool
    ) -> None:
        assert urgent is False, "dashboard reads are routine, never urgent"
        self.calls.append((operation, shop_id, tokens))
        error = self.fail.get(operation)
        if error is not None:
            raise error

    def operations(self) -> list[str]:
        return [operation for operation, _, _ in self.calls]

    async def get_shop_profile(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> ShopProfile:
        self._record("get_shop_profile", shop_id, tokens, urgent=urgent)
        return profile(shop_id)

    async def list_listing_details(
        self,
        shop_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[ListingDetails]:
        self._record("list_listing_details", shop_id, tokens, urgent=urgent)
        return Page(tuple(self.listings[offset : offset + limit]), len(self.listings), offset)

    async def list_sections(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ShopSection, ...]:
        self._record("list_sections", shop_id, tokens, urgent=urgent)
        return (ShopSection(12, "Fake kitchen", 2, 1), ShopSection(11, "Fake mugs", 1, 1))

    async def list_order_receipts(  # noqa: PLR0913 - the port's signature
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        min_last_modified: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[OrderReceipt]:
        self._record("list_order_receipts", shop_id, tokens, urgent=urgent)
        self.windows.append(min_created)
        found = [
            r
            for r in self.receipts
            if (min_created is None or r.created_at >= min_created)
            and (min_last_modified is None or r.updated_at >= min_last_modified)
        ]
        found.sort(key=lambda r: r.created_at, reverse=True)
        return Page(tuple(found[offset : offset + limit]), len(found), offset)

    async def list_reviews(
        self,
        shop_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[ShopReview]:
        self._record("list_reviews", shop_id, tokens, urgent=urgent)
        return Page(tuple(self.reviews[offset : offset + limit]), len(self.reviews), offset)

    async def list_shipping_profiles(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ShippingProfile, ...]:
        self._record("list_shipping_profiles", shop_id, tokens, urgent=urgent)
        return (
            ShippingProfile(
                profile_id=91,
                title="Fake standard",
                origin_country="IL",
                profile_type="manual",
                destinations=(ShippingDestination("US", None, usd(599), usd(200), 7, 14, None),),
                upgrade_count=1,
            ),
        )

    async def list_return_policies(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ReturnPolicy, ...]:
        self._record("list_return_policies", shop_id, tokens, urgent=urgent)
        return (
            ReturnPolicy(
                93, accepts_returns=True, accepts_exchanges=False, return_deadline_days=30
            ),
        )

    async def list_production_partners(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ProductionPartner, ...]:
        self._record("list_production_partners", shop_id, tokens, urgent=urgent)
        return (ProductionPartner(94, "Fake Print Partner", "Fake City"),)
