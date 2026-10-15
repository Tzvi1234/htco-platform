"""Etsy adapter contract tests (slice 4.1) against hand-written, scrubbed fixtures.

No test talks to Etsy: an `httpx.MockTransport` answers with the fixtures in
`tests/fixtures/etsy/`, and a fake clock replaces every sleep.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from htco_application.marketplace import (
    Listing,
    ListingState,
    MarketplaceAuthError,
    MarketplaceConflictError,
    MarketplaceError,
    MarketplaceMoney,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplacePort,
    MarketplaceRateLimitedError,
    MarketplaceRequestError,
    MarketplaceResponseError,
    MarketplaceShop,
    MarketplaceUnavailableError,
    QuotaDeferredError,
    Receipt,
    ReceiptLine,
)
from htco_integrations.etsy import (
    ETSY_BASE_URL,
    EtsyAdapter,
    EtsyConfig,
    EtsyCredentials,
    Timekeeping,
)
from htco_platform.http import new_async_http_client
from tests.log_capture import LogCapture, capture_logs

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "etsy"
KEYSTRING = "fakekeystring"
SHARED_SECRET = "fake-shared-secret"  # noqa: S105 - obviously fake test value
ACCESS_TOKEN = "fake-access-token"  # noqa: S105 - obviously fake test value
SHOP_ID = 10000001
START = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def error_body(status: int) -> dict[str, str]:
    body: dict[str, str] = fixture("errors.json")[str(status)]
    return body


def quota_headers(
    *, per_second: int = 10, this_second: int = 9, per_day: int = 10_000, today: int = 9_000
) -> dict[str, str]:
    return {
        "x-limit-per-second": str(per_second),
        "x-remaining-this-second": str(this_second),
        "x-limit-per-day": str(per_day),
        "x-remaining-today": str(today),
    }


class FakeTime:
    """A monotonic clock and wall clock that only move when the adapter sleeps."""

    def __init__(self) -> None:
        self.seconds = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds - 1_000.0)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.seconds += seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class FakeTokens:
    def __init__(self) -> None:
        self.asked_for: list[int] = []

    async def access_token(self, shop_id: int) -> str:
        self.asked_for.append(shop_id)
        return ACCESS_TOKEN


class Scripted:
    """Answers each request with the next scripted response (the last one repeats)."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(response, Exception):
            raise response
        return response


class Harness:
    def __init__(self, handler: Handler, config: EtsyConfig) -> None:
        self.time = FakeTime()
        self.tokens = FakeTokens()
        self.metrics = InMemoryMetricReader()
        self.client = new_async_http_client(
            tracer_provider=TracerProvider(), transport=httpx.MockTransport(handler)
        )
        self.adapter = EtsyAdapter(
            self.client,
            credentials=EtsyCredentials(keystring=KEYSTRING, shared_secret=SHARED_SECRET),
            access_tokens=self.tokens,
            config=config,
            timekeeping=Timekeeping(
                now=self.time.now, monotonic=self.time.monotonic, sleep=self.time.sleep
            ),
            meter_provider=MeterProvider(metric_readers=[self.metrics]),
        )

    def counter(self, name: str) -> int:
        data = self.metrics.get_metrics_data()
        if data is None:
            return 0
        total = 0
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        total += sum(int(point.value) for point in metric.data.data_points)  # type: ignore[union-attr]
        return total


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Handler, **config: Any) -> Harness:
        return Harness(handler, EtsyConfig(**config))

    return factory


def ok(body: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=body, headers=quota_headers() | headers)


# --- The port ---------------------------------------------------------------------------------


async def test_adapter__satisfies_the_marketplace_port() -> None:
    async with httpx.AsyncClient() as client:
        adapter: MarketplacePort = EtsyAdapter(
            client,
            credentials=EtsyCredentials(keystring=KEYSTRING, shared_secret=SHARED_SECRET),
            access_tokens=FakeTokens(),
        )

        assert adapter.last_seen_quota() is None


def test_credentials__repr__hides_both_values() -> None:
    credentials = EtsyCredentials(keystring=KEYSTRING, shared_secret=SHARED_SECRET)

    assert KEYSTRING not in repr(credentials)
    assert SHARED_SECRET not in repr(credentials)


@pytest.mark.parametrize(("keystring", "secret"), [("", "s"), ("k", ""), ("k:x", "s")])
def test_credentials__blank_or_malformed__are_refused(keystring: str, secret: str) -> None:
    with pytest.raises(ValueError, match="Etsy"):
        EtsyCredentials(keystring=keystring, shared_secret=secret)


# --- Read operations --------------------------------------------------------------------------


async def test_get_shop__sends_key_and_bearer_token__and_maps_the_shop(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        shop = await harness.adapter.get_shop(SHOP_ID)

    assert shop == MarketplaceShop(
        shop_id=10000001,
        name="FakeTestShop",
        currency_code="USD",
        url="https://www.etsy.com/shop/FakeTestShop",
        is_vacation=False,
        active_listing_count=12,
        sold_transaction_count=42,
        created_at=datetime.fromtimestamp(1700000000, UTC),
        updated_at=datetime.fromtimestamp(1760000000, UTC),
    )
    [request] = script.requests
    assert request.method == "GET"
    assert str(request.url) == f"{ETSY_BASE_URL}/shops/{SHOP_ID}"
    assert request.headers["x-api-key"] == f"{KEYSTRING}:{SHARED_SECRET}"
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert harness.tokens.asked_for == [SHOP_ID]


async def test_get_shop__each_request__has_the_configured_timeout(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok(fixture("shop.json")))
    harness = make(script, timeout_seconds=7.5)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    timeout = script.requests[0].extensions["timeout"]
    assert timeout == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}


async def test_list_receipts__filters_by_last_modified__and_maps_receipts(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok(fixture("receipts_page.json")))
    harness = make(script)
    since = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    until = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)

    async with harness.client:
        page = await harness.adapter.list_receipts(
            SHOP_ID, min_last_modified=since, max_last_modified=until, limit=2, offset=4
        )

    [request] = script.requests
    assert request.url.path == f"/v3/application/shops/{SHOP_ID}/receipts"
    assert dict(request.url.params) == {
        "min_last_modified": str(int(since.timestamp())),
        "max_last_modified": str(int(until.timestamp())),
        "limit": "2",
        "offset": "4",
    }
    assert page.total_count == 2
    assert page.offset == 4
    first, second = page.items
    assert first == Receipt(
        receipt_id=3000000001,
        status="paid",
        is_paid=True,
        is_shipped=False,
        created_at=datetime.fromtimestamp(1760000000, UTC),
        updated_at=datetime.fromtimestamp(1760003600, UTC),
        grand_total=MarketplaceMoney(amount=2599, divisor=100, currency_code="USD"),
        lines=(
            ReceiptLine(
                transaction_id=5000000001,
                listing_id=7000000001,
                sku="FAKE-MUG-01",
                quantity=2,
                unit_price=MarketplaceMoney(amount=1000, divisor=100, currency_code="USD"),
            ),
        ),
    )
    assert first.grand_total.as_decimal() == Decimal("25.99")
    assert second.status == "completed"
    assert second.lines[0].listing_id is None
    assert second.lines[0].sku is None


async def test_list_receipts__without_filters__sends_only_the_page(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok({"count": 0, "results": []}))
    harness = make(script)

    async with harness.client:
        page = await harness.adapter.list_receipts(SHOP_ID)

    assert dict(script.requests[0].url.params) == {"limit": "25", "offset": "0"}
    assert page.items == ()
    assert page.next_offset is None


async def test_list_listings__filters_by_state__and_maps_listings(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok(fixture("listings_page.json")))
    harness = make(script)

    async with harness.client:
        page = await harness.adapter.list_listings(
            SHOP_ID, state=ListingState.ACTIVE, limit=100, offset=0
        )

    [request] = script.requests
    assert request.url.path == f"/v3/application/shops/{SHOP_ID}/listings"
    assert dict(request.url.params) == {"state": "active", "limit": "100", "offset": "0"}
    assert page.items == (
        Listing(
            listing_id=7000000001,
            title="Fake test mug",
            state=ListingState.ACTIVE,
            quantity=9,
            price=MarketplaceMoney(amount=1000, divisor=100, currency_code="USD"),
            url="https://www.etsy.com/listing/7000000001/fake-test-mug",
            updated_at=datetime.fromtimestamp(1760000000, UTC),
        ),
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"min_last_modified": datetime(2026, 10, 9)},  # noqa: DTZ001 - naive on purpose
        {
            "min_last_modified": datetime(2026, 10, 10, tzinfo=UTC),
            "max_last_modified": datetime(2026, 10, 9, tzinfo=UTC),
        },
    ],
)
async def test_list_receipts__invalid_arguments__are_refused_before_any_call(
    make: Callable[..., Harness], arguments: dict[str, Any]
) -> None:
    script = Scripted(ok({"count": 0, "results": []}))
    harness = make(script)

    async with harness.client:
        with pytest.raises(ValueError, match=r"limit|offset|timezone|min_last_modified"):
            await harness.adapter.list_receipts(SHOP_ID, **arguments)

    assert script.requests == []


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"count": 1}', b'{"shop_id": "x"}'])
async def test_get_shop__malformed_payload__is_a_response_error_without_retry(
    make: Callable[..., Harness], body: bytes
) -> None:
    script = Scripted(httpx.Response(200, content=body, headers=quota_headers()))
    harness = make(script)

    async with harness.client:
        with pytest.raises(MarketplaceResponseError):
            await harness.adapter.get_shop(SHOP_ID)

    assert len(script.requests) == 1


# --- Typed errors and retries (AC-4.1.1) ------------------------------------------------------


@pytest.mark.ac("AC-4.1.1")
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, MarketplaceRequestError),
        (401, MarketplaceAuthError),
        (403, MarketplacePermissionError),
        (404, MarketplaceNotFoundError),
        (409, MarketplaceConflictError),
        (422, MarketplaceRequestError),
    ],
)
async def test_client_error__maps_to_its_typed_error__and_is_not_retried(
    make: Callable[..., Harness], status: int, error_type: type[MarketplaceError]
) -> None:
    body = fixture("errors.json").get(str(status), {"error": "unprocessable"})
    script = Scripted(httpx.Response(status, json=body, headers=quota_headers()))
    harness = make(script)

    async with harness.client:
        with pytest.raises(error_type) as raised:
            await harness.adapter.get_shop(SHOP_ID)

    assert raised.value.status == status
    assert len(script.requests) == 1
    assert harness.time.sleeps == []


@pytest.mark.ac("AC-4.1.1")
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error__is_retried_with_backoff__then_succeeds(
    make: Callable[..., Harness], status: int
) -> None:
    failure = httpx.Response(status, json=error_body(500), headers=quota_headers())
    script = Scripted(failure, failure, ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        shop = await harness.adapter.get_shop(SHOP_ID)

    assert shop.shop_id == SHOP_ID
    assert len(script.requests) == 3
    assert harness.time.sleeps == [0.5, 1.0]


@pytest.mark.ac("AC-4.1.1")
async def test_server_error__every_attempt__raises_unavailable_after_the_last_attempt(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(httpx.Response(503, json=error_body(503), headers=quota_headers()))
    harness = make(script, max_attempts=4, backoff_max_seconds=1.5)

    async with harness.client:
        with pytest.raises(MarketplaceUnavailableError) as raised:
            await harness.adapter.get_shop(SHOP_ID)

    assert raised.value.status == 503
    assert len(script.requests) == 4
    assert harness.time.sleeps == [0.5, 1.0, 1.5]


@pytest.mark.ac("AC-4.1.1")
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("bad")],
)
async def test_transport_error__is_retried(
    make: Callable[..., Harness], failure: Exception
) -> None:
    script = Scripted(failure, ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    assert len(script.requests) == 2
    assert harness.time.sleeps == [0.5]


@pytest.mark.ac("AC-4.1.1")
async def test_transport_error__every_attempt__raises_unavailable(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(httpx.ConnectTimeout("slow"))
    harness = make(script, max_attempts=2)

    async with harness.client:
        with pytest.raises(MarketplaceUnavailableError) as raised:
            await harness.adapter.get_shop(SHOP_ID)

    assert raised.value.status is None
    assert len(script.requests) == 2


@pytest.mark.ac("AC-4.1.1")
async def test_rate_limited__waits_retry_after__then_succeeds(
    make: Callable[..., Harness],
) -> None:
    limited = httpx.Response(
        429, json=error_body(429), headers=quota_headers(this_second=0) | {"retry-after": "3"}
    )
    script = Scripted(limited, ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    assert len(script.requests) == 2
    assert harness.time.sleeps == [3.0]


@pytest.mark.ac("AC-4.1.1")
async def test_rate_limited__retry_after_as_http_date__is_honoured(
    make: Callable[..., Harness],
) -> None:
    later = "Thu, 15 Oct 2026 12:00:04 GMT"
    limited = httpx.Response(429, json=error_body(429), headers={"retry-after": later})
    script = Scripted(limited, ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    assert harness.time.sleeps == [4.0]


@pytest.mark.ac("AC-4.1.1")
@pytest.mark.parametrize("retry_after", [None, "soon", "-5"])
async def test_rate_limited__without_usable_retry_after__backs_off(
    make: Callable[..., Harness], retry_after: str | None
) -> None:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    limited = httpx.Response(429, json=error_body(429), headers=headers)
    script = Scripted(limited, ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    assert harness.time.sleeps == [0.5]


@pytest.mark.ac("AC-4.1.1")
async def test_rate_limited__every_attempt__raises_rate_limited_with_retry_after(
    make: Callable[..., Harness],
) -> None:
    limited = httpx.Response(429, json=error_body(429), headers={"retry-after": "2"})
    script = Scripted(limited)
    harness = make(script, max_attempts=3)

    async with harness.client:
        with pytest.raises(MarketplaceRateLimitedError) as raised:
            await harness.adapter.get_shop(SHOP_ID)

    assert raised.value.status == 429
    assert raised.value.retry_after_seconds == 2.0
    assert len(script.requests) == 3
    assert harness.time.sleeps == [2.0, 2.0]


@pytest.mark.ac("AC-4.1.1")
async def test_rate_limited__retry_after_beyond_the_cap__raises_without_waiting(
    make: Callable[..., Harness],
) -> None:
    limited = httpx.Response(429, json=error_body(429), headers={"retry-after": "3600"})
    script = Scripted(limited)
    harness = make(script, max_retry_after_seconds=60.0)

    async with harness.client:
        with pytest.raises(MarketplaceRateLimitedError) as raised:
            await harness.adapter.get_shop(SHOP_ID)

    assert raised.value.retry_after_seconds == 3600.0
    assert len(script.requests) == 1
    assert harness.time.sleeps == []


@pytest.mark.ac("AC-4.1.1")
async def test_errors__never_carry_credentials_or_tokens(make: Callable[..., Harness]) -> None:
    script = Scripted(httpx.Response(401, json=error_body(401), headers=quota_headers()))
    harness = make(script)

    async with harness.client:
        with pytest.raises(MarketplaceAuthError) as raised:
            await harness.adapter.get_shop(SHOP_ID)

    text = f"{raised.value!s} {raised.value!r}"
    for secret in (KEYSTRING, SHARED_SECRET, ACCESS_TOKEN):
        assert secret not in text


# --- Rate-limit budget ------------------------------------------------------------------------


async def test_quota_headers__are_kept_as_the_last_seen_snapshot(
    make: Callable[..., Harness],
) -> None:
    headers = quota_headers(per_second=10, this_second=7, per_day=10_000, today=8_123)
    script = Scripted(httpx.Response(200, json=fixture("shop.json"), headers=headers))
    harness = make(script)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    snapshot = harness.adapter.last_seen_quota()
    assert snapshot is not None
    assert (snapshot.limit_per_second, snapshot.remaining_this_second) == (10, 7)
    assert (snapshot.limit_per_day, snapshot.remaining_today) == (10_000, 8_123)
    assert snapshot.observed_at == START


async def test_quota_headers__missing_or_garbled__keep_the_call_working(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(
        httpx.Response(200, json=fixture("shop.json"), headers={"x-remaining-today": "lots"})
    )
    harness = make(script)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)

    snapshot = harness.adapter.last_seen_quota()
    assert snapshot is not None
    assert snapshot.remaining_today is None


async def test_throttle__allows_half_the_per_second_limit__then_waits(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok(fixture("shop.json"), **{"x-limit-per-second": "4"}))
    harness = make(script)

    async with harness.client:
        for _ in range(5):
            await harness.adapter.get_shop(SHOP_ID)

    # Before any header the assumed limit is 10 (5 calls a second); after the first answer the
    # real limit, 4, allows 2 calls a second.
    assert harness.time.sleeps == [1.0, 1.0]
    assert len(script.requests) == 5


async def test_throttle__calls_spread_over_time__never_wait(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(ok(fixture("shop.json")))
    harness = make(script)

    async with harness.client:
        for _ in range(12):
            await harness.adapter.get_shop(SHOP_ID)
            harness.time.advance(0.25)

    assert harness.time.sleeps == []


# --- Daily quota floor (AC-4.1.2) -------------------------------------------------------------


def _low_quota_then_shop(today: int) -> Scripted:
    return Scripted(
        httpx.Response(200, json=fixture("shop.json"), headers=quota_headers(today=today)),
        ok(fixture("shop.json")),
    )


@pytest.mark.ac("AC-4.1.2")
async def test_daily_quota_below_floor__defers_routine_calls__with_warning_and_metric(
    make: Callable[..., Harness],
) -> None:
    script = _low_quota_then_shop(today=499)
    harness = make(script, daily_quota_floor=500)

    with capture_logs() as logs:
        async with harness.client:
            await harness.adapter.get_shop(SHOP_ID)
            with pytest.raises(QuotaDeferredError) as raised:
                await harness.adapter.list_receipts(SHOP_ID)

    assert len(script.requests) == 1
    assert raised.value.remaining_today == 499
    assert raised.value.floor == 500
    deferred = [line for line in logs.lines() if line["event"] == "etsy.call_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["level"] == "warning"
    assert deferred[0]["operation"] == "list_receipts"
    assert deferred[0]["remaining_today"] == 499
    assert harness.counter("htco.etsy.calls_deferred") == 1
    _assert_no_secrets(logs)


@pytest.mark.ac("AC-4.1.2")
async def test_daily_quota_below_floor__is_logged_once_when_first_seen(
    make: Callable[..., Harness],
) -> None:
    script = Scripted(
        httpx.Response(200, json=fixture("shop.json"), headers=quota_headers(today=450))
    )
    harness = make(script, daily_quota_floor=500)

    with capture_logs() as logs:
        async with harness.client:
            await harness.adapter.get_shop(SHOP_ID, urgent=True)
            await harness.adapter.get_shop(SHOP_ID, urgent=True)

    low = [line for line in logs.lines() if line["event"] == "etsy.daily_quota_low"]
    assert len(low) == 1
    assert low[0]["level"] == "warning"
    assert low[0]["remaining_today"] == 450
    assert low[0]["floor"] == 500
    assert harness.counter("htco.etsy.daily_quota_low") == 1
    _assert_no_secrets(logs)


@pytest.mark.ac("AC-4.1.2")
async def test_daily_quota_below_floor__urgent_calls__still_run(
    make: Callable[..., Harness],
) -> None:
    script = _low_quota_then_shop(today=10)
    harness = make(script, daily_quota_floor=500)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)
        shop = await harness.adapter.get_shop(SHOP_ID, urgent=True)

    assert shop.shop_id == SHOP_ID
    assert len(script.requests) == 2


@pytest.mark.ac("AC-4.1.2")
@pytest.mark.parametrize("today", [500, 9_000])
async def test_daily_quota_at_or_above_floor__routine_calls_run(
    make: Callable[..., Harness], today: int
) -> None:
    script = _low_quota_then_shop(today=today)
    harness = make(script, daily_quota_floor=500)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)
        await harness.adapter.list_listings(SHOP_ID)

    assert len(script.requests) == 2


@pytest.mark.ac("AC-4.1.2")
async def test_daily_quota_below_floor__stale_snapshot__lets_a_routine_call_refresh_it(
    make: Callable[..., Harness],
) -> None:
    script = _low_quota_then_shop(today=100)
    harness = make(script, daily_quota_floor=500, quota_snapshot_ttl=timedelta(minutes=15))

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)
        harness.time.advance(15 * 60 + 1)
        await harness.adapter.get_shop(SHOP_ID)

    assert len(script.requests) == 2
    snapshot = harness.adapter.last_seen_quota()
    assert snapshot is not None
    assert snapshot.remaining_today == 9_000


@pytest.mark.ac("AC-4.1.2")
async def test_daily_quota_floor_zero__never_defers(make: Callable[..., Harness]) -> None:
    script = _low_quota_then_shop(today=0)
    harness = make(script, daily_quota_floor=0)

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID)
        await harness.adapter.get_shop(SHOP_ID)

    assert len(script.requests) == 2


def _assert_no_secrets(logs: LogCapture) -> None:
    raw = logs.raw()
    for secret in (KEYSTRING, SHARED_SECRET, ACCESS_TOKEN):
        assert secret not in raw


# --- Configuration ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {"max_attempts": 0},
        {"timeout_seconds": 0},
        {"daily_quota_floor": -1},
        {"backoff_base_seconds": -1},
        {"assumed_limit_per_second": 0},
    ],
)
def test_config__out_of_range__is_refused(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Etsy"):
        EtsyConfig(**config)
