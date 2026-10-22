"""The adapter's receipt history: the whole history (no `min_created`), paged with offsets, the
buyer's country kept (country only), and every call a GET against a strict mock (the owner's live
shop: read only). All values are invented."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from htco_application.shop_dashboard import OrderPeriod, shop_orders, shop_stats
from htco_integrations.etsy import EtsyConfig
from tests.application.test_shop_dashboard import SHOP, World, auth
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, fixture, ok
from tests.integrations.test_etsy_read_only import StrictEtsy
from tests.log_capture import capture_logs
from tests.shop_data_fakes import NOW

pytestmark = pytest.mark.anyio

FAKE_NAME = "Fakename Zzbuyer"


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> Harness:
        return Harness(handler, EtsyConfig())

    return factory


def receipts(**changes: Any) -> dict[str, Any]:
    page: dict[str, Any] = fixture("receipts_page.json")
    page["results"][0].update({"name": FAKE_NAME} | changes)
    return page


async def test_list_order_receipts__whole_history__no_min_created_newest_first(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(receipts()))
    harness = make(etsy)

    async with harness.client:
        page = await harness.adapter.list_order_receipts(
            SHOP_ID, min_created=None, limit=100, offset=200
        )

    [request] = etsy.requests
    assert request.method == "GET"
    assert "min_created" not in request.url.params
    assert request.url.params["sort_on"] == "created"
    assert request.url.params["sort_order"] == "desc"
    assert request.url.params["offset"] == "200"
    assert len(page.items) == 2


async def test_list_order_receipts__no_status_filter_is_sent(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(receipts()))
    harness = make(etsy)

    async with harness.client:
        await harness.adapter.list_order_receipts(SHOP_ID, min_created=None)

    params = etsy.requests[0].url.params
    for status_filter in ("was_paid", "was_shipped", "was_canceled", "was_delivered"):
        assert status_filter not in params


async def test_list_order_receipts__country_only__no_other_address_part(
    make: Callable[..., Harness],
) -> None:
    body = receipts(city="FAKE-CITY-ZZZ", zip="FAKE-ZIP-ZZZ", first_line="FAKE-LINE-ZZZ")
    body["results"][1]["country_iso"] = None
    harness = make(StrictEtsy(ok(body)))

    with capture_logs() as logs:
        async with harness.client:
            page = await harness.adapter.list_order_receipts(SHOP_ID, min_created=None)

    first, second = page.items
    assert first.country_code == "US"
    assert second.country_code is None
    text = repr(page)
    for secret in ("FAKE-CITY-ZZZ", "FAKE-ZIP-ZZZ", "FAKE-LINE-ZZZ", FAKE_NAME):
        assert secret not in text
        assert secret not in logs.raw()


def _history_page(start: int, count: int, total: int) -> dict[str, Any]:
    template = fixture("receipts_page.json")["results"][0]
    results = [
        template | {"receipt_id": 4_000_000_000 + start + n, "name": FAKE_NAME}
        for n in range(count)
    ]
    return {"count": total, "results": results}


class _Routed(StrictEtsy):
    """A strict mock that answers GETs by path (the read order does not matter)."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__()
        self._answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            pytest.fail(f"a {request.method} request reached Etsy: {request.url.path}")
        return self._answer(request)


@pytest.mark.ac("AC-4.5.2")
async def test_all_time_orders_and_stats__only_gets_in_turn_with_offsets(
    make: Callable[..., Harness],
) -> None:
    """The new history reads through the real adapter against a strict mock: profile, then the
    receipts page by page (offset 0, 100, 200), each a GET; any other method fails the test."""
    pages = iter(
        [_history_page(0, 100, 250), _history_page(100, 100, 250), _history_page(200, 50, 250)]
    )

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/receipts"):
            return ok(next(pages))
        return ok(fixture("shop.json"))

    etsy = _Routed(answer)
    harness = make(etsy)
    world = World()
    dashboard = replace(world.dashboard, source=harness.adapter)

    async with harness.client:
        page = await shop_orders(dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
        stats = await shop_stats(dashboard, auth(), SHOP, now=NOW)

    assert page.total == 250
    assert stats.sales is not None
    assert stats.sales.receipts_read == 250
    assert {r.method for r in etsy.requests} == {"GET"}
    receipt_reads = [r for r in etsy.requests if r.url.path.endswith("/receipts")]
    assert [r.url.params["offset"] for r in receipt_reads] == ["0", "100", "200"]
    assert all("min_created" not in r.url.params for r in receipt_reads)
    assert len(etsy.requests) == 4
