"""The adapter's receipts for the stored order history, and the backfill against a strict mock.

The owner's decision of 2026-10-22: the buyer's name and shipping address are kept (for the
stored history), with refunds and their reasons; contact data (email, phone, messages, gift
message, notes) is never read. The backfill reads Etsy with GETs only: the strict mock fails the
test on any other method. All values are invented.
"""

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from htco_application.marketplace import MarketplaceMoney, ShippingAddress
from htco_application.order_history import OrderSync, adapter_pages
from htco_integrations.etsy import EtsyConfig
from tests.application.test_shop_dashboard import SHOP, World
from tests.etsy_fakes import Pinning
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, fixture, ok
from tests.integrations.test_etsy_read_only import StrictEtsy
from tests.log_capture import capture_logs
from tests.order_history_fakes import MemoryOrderStore

pytestmark = pytest.mark.anyio

FAKE_NAME = "Fakefirst Zzlast"
FAKE_EMAIL = "fake.contact.zzz@example.test"
FAKE_PHONE = "+000-FAKE-PHONE"
FAKE_MESSAGE = "FAKE-BUYER-MESSAGE-ZZZ"
FAKE_GIFT = "FAKE-GIFT-MESSAGE-ZZZ"
FAKE_NOTE = "FAKE-SELLER-NOTE-ZZZ"
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def full_receipt(receipt_id: int = 3000000001, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = fixture("receipts_page.json")["results"][0]
    body.update(
        {
            "receipt_id": receipt_id,
            "name": f"  {FAKE_NAME} ",
            "first_line": "FAKE-LINE-ONE",
            "second_line": "FAKE-LINE-TWO",
            "city": "Fakecity",
            "state": "FK",
            "zip": "FAKEZIP9",
            "country_iso": "US",
            "buyer_email": FAKE_EMAIL,
            "payment_email": FAKE_EMAIL,
            "phone": FAKE_PHONE,
            "message_from_buyer": FAKE_MESSAGE,
            "message_from_seller": FAKE_MESSAGE,
            "gift_message": FAKE_GIFT,
            "status": "Partially Refunded",
            "refunds": [
                {
                    "amount": {"amount": 500, "divisor": 100, "currency_code": "USD"},
                    "created_timestamp": 1760100000,
                    "reason": "damaged",
                    "note_from_issuer": FAKE_NOTE,
                    "status": "refunded",
                }
            ],
        }
        | changes
    )
    return body


def _values(value: object) -> list[str]:
    """Every string anywhere in a dataclass tree (repr-hidden fields included)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [s for f in dataclasses.fields(value) for s in _values(getattr(value, f.name))]
    if isinstance(value, tuple | list):
        return [s for item in value for s in _values(item)]
    return [value] if isinstance(value, str) else []


async def test_order_receipt__keeps_name_address_and_refunds__never_contact_data() -> None:
    etsy = StrictEtsy(ok({"count": 1, "results": [full_receipt()]}))
    harness = Harness(etsy, EtsyConfig())

    with capture_logs() as logs:
        async with harness.client:
            page = await harness.adapter.list_order_receipts(SHOP_ID, min_created=SINCE)

    [receipt] = page.items
    assert receipt.buyer_name == FAKE_NAME
    assert receipt.buyer_display_name == "Fakefirst Z."
    assert receipt.shipping_address == ShippingAddress(
        "FAKE-LINE-ONE", "FAKE-LINE-TWO", "Fakecity", "FK", "FAKEZIP9", "US"
    )
    [refund] = receipt.refunds
    assert refund.amount == MarketplaceMoney(500, 100, "USD")
    assert (refund.reason, refund.status, refund.note_from_issuer) == (
        "damaged",
        "refunded",
        FAKE_NOTE,
    )
    assert receipt.status == "partially refunded"
    kept = _values(receipt)
    for contact in (FAKE_EMAIL, FAKE_PHONE, FAKE_MESSAGE, FAKE_GIFT):
        assert not any(contact in value for value in kept), contact
    text = repr(page)
    for personal in (FAKE_NAME, "FAKE-LINE-ONE", "Fakecity", "FAKEZIP9", FAKE_NOTE):
        assert personal not in text
        assert personal not in logs.raw()


async def test_order_receipt__no_address_fields_shared__no_address() -> None:
    body = full_receipt(first_line=None, second_line=None, city=None, state=None, zip=None)
    harness = Harness(StrictEtsy(ok({"count": 1, "results": [body]})), EtsyConfig())

    async with harness.client:
        page = await harness.adapter.list_order_receipts(SHOP_ID, min_created=SINCE)

    assert page.items[0].shipping_address is None


async def test_backfill_through_the_adapter__gets_only_and_every_page() -> None:
    """The backfill and the incremental sync against the strict mock: three GETs, nothing
    else (any other method fails the test)."""
    world = World()
    etsy = StrictEtsy(
        ok({"count": 3, "results": [full_receipt(1), full_receipt(2)]}),
        ok({"count": 3, "results": [full_receipt(3)]}),
        ok({"count": 1, "results": [full_receipt(1)]}),
    )
    harness = Harness(etsy, EtsyConfig())
    store = MemoryOrderStore()
    sync = OrderSync(
        store=store, pages=adapter_pages(harness.adapter, Pinning()), clock=world.clock
    )

    with capture_logs() as logs:
        async with harness.client:
            await sync.run(world.vault.rows[SHOP])

    assert [r.method for r in etsy.requests] == ["GET", "GET", "GET"]
    assert [r.url.params["offset"] for r in etsy.requests] == ["0", "2", "0"]
    assert "min_created" not in etsy.requests[0].url.params
    assert "min_last_modified" not in etsy.requests[0].url.params
    # The incremental sync reads what changed in the last 30 days, whenever it was ordered: a
    # late refund or cancellation of an older order reaches the store (post-merge review).
    assert "min_created" not in etsy.requests[2].url.params
    assert "min_last_modified" in etsy.requests[2].url.params
    assert sorted(receipt_id for _, receipt_id in store.orders) == [1, 2, 3]
    for personal in (FAKE_NAME, "FAKE-LINE-ONE", FAKE_NOTE):
        assert personal not in logs.raw()
