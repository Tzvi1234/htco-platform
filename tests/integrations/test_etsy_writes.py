"""The adapter's two owner-triggered writes (slices 6.6 and 6.8, vacation mode from 7.12).

Against a mock transport only: no test reaches Etsy (AC-6.8.3). Each write is one request, never
retried (Etsy's writes are not idempotent, and `createReceiptShipment` emails the buyer), and its
form body never reaches a log line or an error.
"""

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from htco_application.marketplace import (
    MarketplacePermissionError,
    MarketplaceUnavailableError,
)
from htco_integrations.etsy import ETSY_BASE_URL, EtsyConfig
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, Scripted, error_body, ok
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

RECEIPT = 4000000001
TRACKING = "LX123456789IL"  # made up: it must never reach a log line or an error


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Callable[[httpx.Request], httpx.Response], **config: Any) -> Harness:
        return Harness(handler, EtsyConfig(**config))

    return factory


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


async def test_create_receipt_shipment__one_post_with_the_tracking_form(
    make: Any,
) -> None:
    etsy = Scripted(ok({"receipt_id": RECEIPT}))
    harness: Harness = make(etsy)

    await harness.adapter.create_receipt_shipment(
        SHOP_ID, RECEIPT, tracking_code=TRACKING, carrier_name="israel-post"
    )

    [request] = etsy.requests
    assert request.method == "POST"
    assert str(request.url) == f"{ETSY_BASE_URL}/shops/{SHOP_ID}/receipts/{RECEIPT}/tracking"
    assert form(request) == {"tracking_code": [TRACKING], "carrier_name": ["israel-post"]}
    assert request.headers["authorization"].startswith("Bearer ")


async def test_update_shop_vacation__one_put_with_the_vacation_fields(make: Any) -> None:
    etsy = Scripted(ok({"shop_id": SHOP_ID}))
    harness: Harness = make(etsy)

    await harness.adapter.update_shop_vacation(
        SHOP_ID, is_vacation=True, vacation_message="Back on 2 November"
    )

    [request] = etsy.requests
    assert (request.method, str(request.url)) == ("PUT", f"{ETSY_BASE_URL}/shops/{SHOP_ID}")
    assert form(request) == {"is_vacation": ["true"], "vacation_message": ["Back on 2 November"]}


async def test_update_shop_vacation__off_sends_no_message(make: Any) -> None:
    etsy = Scripted(ok({"shop_id": SHOP_ID}))
    harness: Harness = make(etsy)

    await harness.adapter.update_shop_vacation(SHOP_ID, is_vacation=False, vacation_message=None)

    assert form(etsy.requests[0]) == {"is_vacation": ["false"]}


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(503, json=error_body(503)), httpx.ConnectError("refused")],
    ids=["5xx", "transport"],
)
async def test_a_write__is_never_retried(make: Any, answer: httpx.Response | Exception) -> None:
    etsy = Scripted(answer, ok({"receipt_id": RECEIPT}))
    harness: Harness = make(etsy, max_attempts=3)

    with pytest.raises(MarketplaceUnavailableError):
        await harness.adapter.create_receipt_shipment(
            SHOP_ID, RECEIPT, tracking_code=TRACKING, carrier_name="israel-post"
        )

    assert len(etsy.requests) == 1


async def test_etsys_refusal__a_typed_error_without_the_form(make: Any) -> None:
    etsy = Scripted(httpx.Response(403, json=error_body(403)))
    harness: Harness = make(etsy)

    with capture_logs() as logs, pytest.raises(MarketplacePermissionError) as refused:
        await harness.adapter.create_receipt_shipment(
            SHOP_ID, RECEIPT, tracking_code=TRACKING, carrier_name="israel-post"
        )

    assert TRACKING not in str(refused.value)
    assert TRACKING not in logs.raw()


@pytest.mark.parametrize(
    ("receipt_id", "code", "carrier"),
    [(0, TRACKING, "israel-post"), (RECEIPT, " ", "israel-post"), (RECEIPT, TRACKING, "")],
)
async def test_a_malformed_write__refused_before_any_request(
    make: Any, receipt_id: int, code: str, carrier: str
) -> None:
    etsy = Scripted(ok({}))
    harness: Harness = make(etsy)

    with pytest.raises(ValueError, match="Etsy"):
        await harness.adapter.create_receipt_shipment(
            SHOP_ID, receipt_id, tracking_code=code, carrier_name=carrier
        )

    assert etsy.requests == []
