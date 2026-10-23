"""The adapter's receipt for Etsy Order Intake (slice 5.4): one GET against a strict mock, and
a receipt carrying fake contact values keeps none of them (ADR-0018, AC-5.4.6)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from htco_application.order_receipts import receipt_to_json
from htco_integrations.etsy import EtsyConfig
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, fixture, ok
from tests.integrations.test_etsy_read_only import StrictEtsy
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

# Contact values Etsy could send: none may survive into a receipt, a log or the action output.
CONTACT = {
    "buyer_email": "FAKE-CONTACT-EMAIL-ZZZ",
    "seller_email": "FAKE-SELLER-EMAIL-ZZZ",
    "message_from_buyer": "FAKE-CONTACT-MESSAGE-ZZZ",
    "message_from_seller": "FAKE-SELLER-MESSAGE-ZZZ",
    "gift_message": "FAKE-CONTACT-GIFT-ZZZ",
    "phone": "FAKE-CONTACT-PHONE-ZZZ",
}


def _receipt(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = fixture("receipts_page.json")["results"][0]
    return body | changes


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> Harness:
        return Harness(handler, EtsyConfig())

    return factory


async def test_get_order_receipt__one_get_by_id(make: Callable[..., Harness]) -> None:
    body = _receipt()
    etsy = StrictEtsy(ok(body))
    harness = make(etsy)

    async with harness.client:
        receipt = await harness.adapter.get_order_receipt(SHOP_ID, int(body["receipt_id"]))

    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/shops/{SHOP_ID}/receipts/{body['receipt_id']}")
    assert receipt.receipt_id == body["receipt_id"]


@pytest.mark.ac("AC-5.4.6")
async def test_get_order_receipt__fake_contact_values_are_never_kept(
    make: Callable[..., Harness],
) -> None:
    body = _receipt(**CONTACT)
    harness = make(StrictEtsy(ok(body)))

    with capture_logs() as logs:
        async with harness.client:
            receipt = await harness.adapter.get_order_receipt(SHOP_ID, int(body["receipt_id"]))

    kept = json.dumps(receipt_to_json(receipt)) + repr(receipt)
    for value in CONTACT.values():
        assert value not in kept
        assert value not in logs.raw()


async def test_get_order_receipt__an_invalid_id_is_refused_before_any_call(
    make: Callable[..., Harness],
) -> None:
    harness = make(StrictEtsy())
    async with harness.client:
        with pytest.raises(ValueError, match="receipt_id"):
            await harness.adapter.get_order_receipt(SHOP_ID, 0)


async def test_list_order_receipts__open_orders_filter_is_one_get(
    make: Callable[..., Harness],
) -> None:
    """Reconciliation's paid, unshipped orders (slice 5.5): Etsy's own filters, no period."""
    etsy = StrictEtsy(ok(fixture("receipts_page.json")))
    harness = make(etsy)

    async with harness.client:
        await harness.adapter.list_order_receipts(
            SHOP_ID, min_created=None, was_paid=True, was_shipped=False
        )

    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.params["was_paid"] == "true"
    assert request.url.params["was_shipped"] == "false"
    assert "min_created" not in request.url.params
    assert "min_last_modified" not in request.url.params
