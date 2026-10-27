"""The Etsy adapter never writes to a shop (owner's rule, AC-4.5.2), and the receipt address check
used by the capability probes (slice 4.5) reports presence only, never values.

The connected pilot shop is live with real buyers: HTCO may only read. Two independent guards
refuse any other method towards Etsy's shop API: the adapter's own check before it asks for a
token, and a request hook on the HTTP client that stops the request before it leaves the process.
Both need an explicit per-call allow flag that no code path sets (the last test scans the source).
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from htco_application.marketplace import (
    MarketplaceAuthError,
    MarketplacePermissionError,
    MarketplaceWriteRefusedError,
    ReceiptAddressCheck,
)
from htco_integrations.etsy import (
    ETSY_BASE_URL,
    READ_ONLY_METHODS,
    EtsyAdapter,
    EtsyConfig,
    EtsyCredentials,
    Timekeeping,
)
from tests.integrations.test_etsy_adapter import (
    SHOP_ID,
    Harness,
    Scripted,
    fixture,
    ok,
    quota_headers,
)
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
WRITE_ALLOWED = re.compile(r"allow_write\s*=\s*True")
WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"]
# Obviously fake address values: they must never reach a result, a log line or an error.
FAKE_LINE = "FAKE-LINE-ONE-ZZZ"
FAKE_CITY = "FAKE-CITY-ZZZ"
FAKE_NAME = "FAKE-BUYER-NAME-ZZZ"


class StrictEtsy:
    """Answers GETs from a script and fails the test on any other method."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._get = Scripted(*responses) if responses else None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            pytest.fail(f"a {request.method} request reached Etsy: {request.url.path}")
        if self._get is None:
            pytest.fail("no Etsy call was expected")
        return self._get(request)


@pytest.fixture
def make() -> Callable[..., Harness]:
    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> Harness:
        return Harness(handler, EtsyConfig())

    return factory


# --- Only GET ----------------------------------------------------------------------------------


def test_read_only_methods__are_get_alone() -> None:
    assert frozenset({"GET"}) == READ_ONLY_METHODS


@pytest.mark.ac("AC-4.5.2")
@pytest.mark.ac("AC-4.5.2")
@pytest.mark.parametrize("method", WRITE_METHODS)
async def test_adapter_request__non_get__refused_before_any_token_or_call(
    make: Callable[..., Harness], method: str
) -> None:
    etsy = StrictEtsy()
    harness = make(etsy)

    async with harness.client:
        with pytest.raises(MarketplaceWriteRefusedError) as refused:
            await harness.adapter._request(
                method,
                "create_receipt_shipment",
                SHOP_ID,
                f"/shops/{SHOP_ID}/receipts/1/tracking",
                {},
                urgent=True,
            )

    assert etsy.requests == []
    assert harness.tokens.asked_for == []
    assert method in str(refused.value)


@pytest.mark.parametrize("method", WRITE_METHODS)
async def test_http_client_hook__non_get_to_the_shop_api__never_leaves_the_process(
    make: Callable[..., Harness], method: str
) -> None:
    """Even code that bypasses the adapter and uses its client directly cannot write."""
    etsy = StrictEtsy()
    harness = make(etsy)

    async with harness.client:
        with pytest.raises(MarketplaceWriteRefusedError):
            await harness.client.request(method, f"{ETSY_BASE_URL}/shops/{SHOP_ID}", json={})
        with pytest.raises(MarketplaceWriteRefusedError):
            # tokenScopes is a POST too: it is not used (stored scopes are probed instead).
            await harness.client.request(method, f"{ETSY_BASE_URL}/scopes", data={"token": "x"})

    assert etsy.requests == []


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize(
    "url",
    [
        "https://api.etsy.com/v3/application/shops/10000001",  # the API's other host
        "https://www.etsy.com/your/shops/me/tools/listings",
        "https://etsy.com/x",
        "https://openapi.etsy.com/v2/shops/10000001",  # another version, outside the base URL
    ],
)
async def test_http_client_hook__non_get_to_any_etsy_host__never_leaves_the_process(
    make: Callable[..., Harness], method: str, url: str
) -> None:
    """The live shop is guarded on every Etsy host, not only under the adapter's base URL."""
    etsy = StrictEtsy()
    harness = make(etsy)

    async with harness.client:
        with pytest.raises(MarketplaceWriteRefusedError):
            await harness.client.request(method, url, data={"x": "1"})

    assert etsy.requests == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_http_client_hook__the_token_endpoint_takes_a_post_only(
    make: Callable[..., Harness], method: str
) -> None:
    etsy = StrictEtsy()
    harness = make(etsy)

    async with harness.client:
        with pytest.raises(MarketplaceWriteRefusedError):
            await harness.client.request(method, "https://api.etsy.com/v3/public/oauth/token")

    assert etsy.requests == []


async def test_http_client_hook__leaves_other_hosts_alone(make: Callable[..., Harness]) -> None:
    """The OAuth token endpoint (a POST on another path) and other services still work."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        return httpx.Response(200, json={})

    harness = make(handler)
    async with harness.client:
        await harness.client.post("https://api.etsy.com/v3/public/oauth/token", data={})
        await harness.client.post("https://example.supabase.co/auth/v1/token", data={})

    assert seen == [
        "POST https://api.etsy.com/v3/public/oauth/token",
        "POST https://example.supabase.co/auth/v1/token",
    ]


async def test_http_client_hook__installed_once_per_client(make: Callable[..., Harness]) -> None:
    harness = make(StrictEtsy())
    hooks_before = len(harness.client.event_hooks["request"])
    EtsyAdapter(
        harness.client,
        credentials=EtsyCredentials(keystring="k", shared_secret="s"),  # noqa: S106 - fake
        access_tokens=harness.tokens,
    )

    assert len(harness.client.event_hooks["request"]) == hooks_before
    await harness.client.aclose()


async def test_write_refusal__is_logged_without_a_token_or_payload(
    make: Callable[..., Harness],
) -> None:
    harness = make(StrictEtsy())
    with capture_logs() as logs:
        async with harness.client:
            with pytest.raises(MarketplaceWriteRefusedError):
                await harness.client.post(f"{ETSY_BASE_URL}/shops/{SHOP_ID}", json={"x": FAKE_NAME})

    [event] = [e for e in logs.lines() if e["event"] == "etsy.write_refused"]
    assert event["method"] == "POST"
    assert FAKE_NAME not in logs.raw()


@pytest.mark.ac("AC-4.5.2")
def test_source__only_the_two_owner_triggered_writes_allow_a_write() -> None:
    """The allow flag is set by exactly the owner's two writes (slices 6.6 and 6.8): the
    tracking upload and vacation mode, in the adapter. Nothing else sets it, and the adapter calls
    the client only through the guarded `request`."""
    sources = [
        *ROOT.joinpath("packages").rglob("*.py"),
        *ROOT.joinpath("apps").rglob("*.py"),
        *ROOT.joinpath("workers").rglob("*.py"),
    ]
    adapter = ROOT / "packages" / "integrations" / "src" / "htco_integrations" / "etsy.py"
    offenders = [
        str(path.relative_to(ROOT))
        for path in sources
        if ".venv" not in path.parts
        and path.resolve() != adapter.resolve()
        and WRITE_ALLOWED.search(path.read_text("utf-8"))
    ]
    assert offenders == []
    source = adapter.read_text("utf-8")
    assert len(WRITE_ALLOWED.findall(source)) == 2  # create_receipt_shipment, update_shop_vacation
    assert re.findall(r"self\._client\.(\w+)\(", source) == ["request"]


# --- The receipt address check (presence only) ------------------------------------------------


def _receipt_page(**fields: Any) -> dict[str, Any]:
    page: dict[str, Any] = fixture("receipts_page.json")
    receipt = page["results"][0] | {"name": FAKE_NAME} | fields
    return {"count": 7, "results": [receipt]}


@pytest.mark.ac("AC-4.5.2")
async def test_check_receipt_addresses__fields_present__reports_presence_only(
    make: Callable[..., Harness],
) -> None:
    etsy = StrictEtsy(ok(_receipt_page(first_line=FAKE_LINE, city=FAKE_CITY)))
    harness = make(etsy)

    with capture_logs() as logs:
        async with harness.client:
            check = await harness.adapter.check_receipt_addresses(SHOP_ID)

    assert check == ReceiptAddressCheck(receipts_seen=1, address_present=True)
    [request] = etsy.requests
    assert request.method == "GET"
    assert request.url.path.endswith(f"/shops/{SHOP_ID}/receipts")
    assert request.url.params["limit"] == "1"
    for value in (FAKE_LINE, FAKE_CITY, FAKE_NAME):
        assert value not in repr(check)
        assert value not in logs.raw()


@pytest.mark.parametrize(
    "fields",
    [
        {"first_line": None, "city": None},
        {"first_line": "", "city": FAKE_CITY},
        {"first_line": FAKE_LINE, "city": "  "},
        {"first_line": FAKE_LINE},  # city missing from the answer
    ],
)
async def test_check_receipt_addresses__fields_hidden__absent(
    make: Callable[..., Harness], fields: dict[str, Any]
) -> None:
    page = _receipt_page(**fields)
    if "city" not in fields:
        del page["results"][0]["city"]
    harness = make(StrictEtsy(ok(page)))

    async with harness.client:
        check = await harness.adapter.check_receipt_addresses(SHOP_ID)

    assert check == ReceiptAddressCheck(receipts_seen=1, address_present=False)


async def test_check_receipt_addresses__no_receipt__presence_unknown(
    make: Callable[..., Harness],
) -> None:
    harness = make(StrictEtsy(ok({"count": 0, "results": []})))

    async with harness.client:
        check = await harness.adapter.check_receipt_addresses(SHOP_ID)

    assert check == ReceiptAddressCheck(receipts_seen=0, address_present=None)


async def test_check_receipt_addresses__forbidden__typed_error(
    make: Callable[..., Harness],
) -> None:
    harness = make(StrictEtsy(httpx.Response(403, json={"error": "x"}, headers=quota_headers())))

    async with harness.client:
        with pytest.raises(MarketplacePermissionError):
            await harness.adapter.check_receipt_addresses(SHOP_ID)


# --- Codex review of #49: a disconnect stops calls already under way ----------------------------


class DisconnectedAfterFirstToken:
    """Hands out one token, then answers as the provider does once the shop is disconnected."""

    def __init__(self) -> None:
        self.asked = 0

    async def access_token(self, shop_id: int) -> str:
        self.asked += 1
        if self.asked > 1:
            raise MarketplaceAuthError(f"Etsy shop {shop_id} is not connected")
        return "fake-access-token"


@pytest.mark.ac("AC-4.7.1")
async def test_retry_after_a_disconnect__no_further_request() -> None:
    """Each attempt asks for the token again (after the throttle wait), so a call that is backing
    off when the shop is disconnected never sends its retry."""
    etsy = StrictEtsy(httpx.Response(503, json={"error": "x"}, headers=quota_headers()))
    harness = Harness(etsy, EtsyConfig())
    tokens = DisconnectedAfterFirstToken()
    adapter = EtsyAdapter(
        harness.client,
        credentials=EtsyCredentials(keystring="k", shared_secret="s"),  # noqa: S106 - fake
        access_tokens=tokens,
        timekeeping=Timekeeping(
            now=harness.time.now, monotonic=harness.time.monotonic, sleep=harness.time.sleep
        ),
    )

    async with harness.client:
        with pytest.raises(MarketplaceAuthError):
            await adapter.get_shop(SHOP_ID, urgent=True)

    assert len(etsy.requests) == 1
    assert tokens.asked == 2


async def test_reads__a_per_call_token_source__is_used_instead_of_the_adapters(
    make: Callable[..., Harness],
) -> None:
    class Pinned:
        def __init__(self) -> None:
            self.asked: list[int] = []

        async def access_token(self, shop_id: int) -> str:
            self.asked.append(shop_id)
            return "pinned-token"

    etsy = StrictEtsy(
        ok(fixture("shop.json")),
        ok({"count": 0, "results": []}),
        ok(fixture("listings_page.json")),
    )
    harness = make(etsy)
    pinned = Pinned()

    async with harness.client:
        await harness.adapter.get_shop(SHOP_ID, urgent=True, tokens=pinned)
        await harness.adapter.check_receipt_addresses(SHOP_ID, tokens=pinned)
        await harness.adapter.list_listings(SHOP_ID, limit=1, tokens=pinned)

    assert pinned.asked == [SHOP_ID] * 3
    assert harness.tokens.asked_for == []
    assert {r.headers["authorization"] for r in etsy.requests} == {"Bearer pinned-token"}
