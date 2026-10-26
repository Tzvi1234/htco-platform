"""Etsy Open API v3 adapter: the `MarketplacePort` read operations (slice 4.1, ADR-0008).

Every request carries `x-api-key: <keystring>:<shared secret>` (the shared secret is mandatory
since February 2026) and the shop's OAuth access token, supplied per call by an
`AccessTokenProvider` (the vault, refreshed on demand: `htco_application.etsy_tokens`). The connect
flow's shop lookup (`shop_of_user`) passes the token it has just received instead.

Rate-limit budget (per app, not per shop): each answer's `x-limit-per-second`,
`x-remaining-this-second`, `x-limit-per-day` and `x-remaining-today` headers are kept as the last
seen `QuotaSnapshot`. Calls are throttled to half the per-second limit. Below the configured daily
floor, routine calls are deferred with a warning and a metric; urgent calls still run. A snapshot
older than `quota_snapshot_ttl` no longer defers, so one routine call can refresh it after Etsy's
window has moved on.

Read only, but for two writes (the owner's rule: connected shops are live, with real buyers).
Every call goes through one choke point, `_request`, which refuses any method but GET before a
token is fetched unless the caller sets the `allow_write` flag for that call. Only the two W3
writes set it: `create_receipt_shipment` and `update_shop_vacation` (slices 6.6 and 6.8, the
owner's decision of 2026-10-26: built with every write flag off, never sent by a test, a script
or an agent). Only the Action Gateway's API channel calls them
(`tests/architecture/test_gateway_bypass.py`), on the Owner's own press after a step-up or on a
request the Owner approved, once the granted scope and the write flag are checked. As a second,
independent guard the adapter installs a request hook on its HTTP client that stops any non-GET
request to any Etsy host (`etsy.com` and every `*.etsy.com`: the shop API on openapi.etsy.com and
api.etsy.com, the website) and to the configured base URL, before it leaves the process, even if
some code used the client directly. The one exception is the OAuth token exchange, a POST to
exactly `api.etsy.com/v3/public/oauth/token`, which changes no shop. Etsy's `tokenScopes` is a
POST, so it is not used: the capability probes read the stored scopes.

Retries: only GETs are made here, so 429 (honouring `retry-after`), 5xx and transport failures
(timeouts, refused connections) are retried with capped exponential backoff. Other 4xx answers
map to typed errors at once. Write operations, which are not idempotent at Etsy, must not reuse
this retry loop.

Nothing here logs or raises with the API key, the shared secret, an access token or a payload.
"""

import hashlib
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, NoReturn

import anyio
import httpx
import structlog
from opentelemetry import metrics

from htco_application.marketplace import (
    AccessTokenProvider,
    CredentialsRejectionSink,
    InventorySnapshot,
    Listing,
    ListingDetails,
    ListingImage,
    ListingSnapshot,
    ListingState,
    MarketplaceConflictError,
    MarketplaceCredentialsRejectedError,
    MarketplaceError,
    MarketplaceMoney,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceRequestError,
    MarketplaceResponseError,
    MarketplaceShop,
    MarketplaceUnavailableError,
    MarketplaceWriteRefusedError,
    OrderLine,
    OrderReceipt,
    OrderRefund,
    OrderShipment,
    Page,
    ProductionPartner,
    ProductStock,
    QuotaDeferredError,
    QuotaSnapshot,
    Receipt,
    ReceiptAddressCheck,
    ReceiptLine,
    ReturnPolicy,
    ShippingAddress,
    ShippingDestination,
    ShippingProfile,
    ShopProfile,
    ShopReview,
    ShopSection,
    display_name,
)

ETSY_BASE_URL = "https://openapi.etsy.com/v3/application"
MAX_PAGE_SIZE = 100
# The only methods sent to Etsy's shop API. Anything else needs an explicit per-call allow flag.
READ_ONLY_METHODS = frozenset({"GET"})
# Request extension that lets one request past the hook; set only when `allow_write` is set.
_WRITE_ALLOWED = "htco.etsy.write_allowed"
# The only non-GET request to an Etsy host the client hook lets through without that flag: the
# OAuth token exchange (`htco_integrations.etsy_oauth`), which changes no shop.
_TOKEN_EXCHANGE = ("POST", "api.etsy.com", "/v3/public/oauth/token")
# Fields whose presence on a receipt shows Etsy shares the buyer's shipping address.
_ADDRESS_FIELDS = ("first_line", "city")

_QUOTA_HEADERS = (
    "x-limit-per-second",
    "x-remaining-this-second",
    "x-limit-per-day",
    "x-remaining-today",
)

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EtsyCredentials:
    """The app's keystring and shared secret; both are secrets and stay out of `repr`."""

    keystring: str = field(repr=False)
    shared_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.keystring.strip() or not self.shared_secret.strip():
            raise ValueError("Etsy keystring and shared secret are both required")
        if ":" in self.keystring:
            raise ValueError("Etsy keystring must not contain ':'")

    def api_key_header(self) -> str:
        return f"{self.keystring}:{self.shared_secret}"

    @property
    def fingerprint(self) -> str:
        """`app_key_fingerprint` of the keystring: which app the stored tokens were issued for."""
        return app_key_fingerprint(self.keystring)


def app_key_fingerprint(keystring: str) -> str:
    """The first 16 hex characters of the keystring's SHA-256: enough to tell two apps apart,
    never the key itself. Tokens are issued per app, so a connection whose fingerprint differs
    from the configured key's cannot work and is treated as `needs_reauth` without calling Etsy.
    Whitespace around the key is ignored: the same key re-entered with a trailing newline is the
    same app, and must not mark every healthy connection `needs_reauth`.
    """
    return hashlib.sha256(keystring.strip().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class EtsyConfig:
    base_url: str = ETSY_BASE_URL
    timeout_seconds: float = 10.0
    # Attempts per call, the first included.
    max_attempts: int = 4
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    # A 429 asking us to wait longer than this fails at once instead of holding the caller.
    max_retry_after_seconds: float = 60.0
    # Below this many requests left today, routine calls are deferred (0 turns deferral off).
    daily_quota_floor: int = 500
    quota_snapshot_ttl: timedelta = timedelta(minutes=15)
    # Used until Etsy reports the real per-second limit.
    assumed_limit_per_second: int = 10

    def __post_init__(self) -> None:
        problems = [
            name
            for name, ok in (
                ("timeout_seconds", self.timeout_seconds > 0),
                ("max_attempts", self.max_attempts >= 1),
                ("backoff_base_seconds", self.backoff_base_seconds >= 0),
                ("backoff_max_seconds", self.backoff_max_seconds >= 0),
                ("max_retry_after_seconds", self.max_retry_after_seconds >= 0),
                ("daily_quota_floor", self.daily_quota_floor >= 0),
                ("quota_snapshot_ttl", self.quota_snapshot_ttl > timedelta(0)),
                ("assumed_limit_per_second", self.assumed_limit_per_second >= 1),
            )
            if not ok
        ]
        if problems:
            raise ValueError(f"Etsy configuration out of range: {', '.join(problems)}")


def _wall_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Timekeeping:
    """Clocks and sleep, injected so tests never wait."""

    now: Callable[[], datetime] = _wall_clock
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep


class EtsyAdapter:
    """`MarketplacePort` on Etsy. One instance per app key, shared by every shop."""

    def __init__(  # noqa: PLR0913 - collaborators are injected separately on purpose
        self,
        client: httpx.AsyncClient,
        *,
        credentials: EtsyCredentials,
        access_tokens: AccessTokenProvider,
        config: EtsyConfig | None = None,
        timekeeping: Timekeeping | None = None,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._tokens = access_tokens
        self._config = config or EtsyConfig()
        self._time = timekeeping or Timekeeping()
        self._timeout = httpx.Timeout(self._config.timeout_seconds)
        meter = (meter_provider or metrics.get_meter_provider()).get_meter(__name__)
        self._deferred_calls = meter.create_counter(
            "htco.etsy.calls_deferred",
            unit="{call}",
            description="Routine Etsy calls deferred because the daily budget is below the floor",
        )
        self._low_quota = meter.create_counter(
            "htco.etsy.daily_quota_low",
            unit="{event}",
            description="Times Etsy's remaining daily budget fell below the floor",
        )
        self._quota: QuotaSnapshot | None = None
        self._low_quota_reported = False
        # One routine call may refresh a stale low snapshot; the others wait for its answer.
        self._refresh_in_flight = False
        self._sent: deque[float] = deque()
        self._throttle = anyio.Lock()
        _install_write_guard(client, self._config.base_url)

    # --- MarketplacePort ----------------------------------------------------------------------

    async def get_shop(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> MarketplaceShop:
        body = await self._request(
            "GET", "get_shop", shop_id, f"/shops/{shop_id}", {}, urgent=urgent, tokens=tokens
        )
        return _parse("get_shop", _shop, body)

    async def list_receipts(  # noqa: PLR0913 - each keyword is one filter of the same query
        self,
        shop_id: int,
        *,
        min_last_modified: datetime | None = None,
        max_last_modified: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Receipt]:
        _check_page(limit, offset)
        params: dict[str, int] = {}
        if min_last_modified is not None:
            params["min_last_modified"] = _epoch(min_last_modified, "min_last_modified")
        if max_last_modified is not None:
            params["max_last_modified"] = _epoch(max_last_modified, "max_last_modified")
        if (
            min_last_modified is not None
            and max_last_modified is not None
            and min_last_modified > max_last_modified
        ):
            raise ValueError("min_last_modified must not be after max_last_modified")
        params |= {"limit": limit, "offset": offset}
        body = await self._request(
            "GET", "list_receipts", shop_id, f"/shops/{shop_id}/receipts", params, urgent=urgent
        )
        return _parse("list_receipts", lambda data: _page(data, _receipt, offset), body)

    async def get_receipt(self, shop_id: int, receipt_id: int, *, urgent: bool = False) -> Receipt:
        """One receipt by ID (`getShopReceipt`, a GET): order facts only, like `list_receipts`."""
        if receipt_id < 1:
            raise ValueError("receipt_id must be a positive Etsy receipt ID")
        body = await self._request(
            "GET", "get_receipt", shop_id, f"/shops/{shop_id}/receipts/{receipt_id}", {},
            urgent=urgent,
        )  # fmt: skip
        return _parse("get_receipt", _receipt, body)

    async def get_listing(self, shop_id: int, listing_id: int) -> ListingSnapshot:
        """One listing by ID (`getListing`, a GET), with the shop it belongs to: the webhook
        re-read (slice 5.3) checks that it is the shop the webhook named."""
        if listing_id < 1:
            raise ValueError("listing_id must be a positive Etsy listing ID")
        body = await self._request(
            "GET", "get_listing", shop_id, f"/listings/{listing_id}", {}, urgent=False
        )
        return _parse("get_listing", _listing_snapshot, body)

    async def get_listing_inventory(self, shop_id: int, listing_id: int) -> InventorySnapshot:
        """A listing's products and stock (`getListingInventory`, a GET, `listings_r`)."""
        if listing_id < 1:
            raise ValueError("listing_id must be a positive Etsy listing ID")
        body = await self._request(
            "GET", "get_listing_inventory", shop_id, f"/listings/{listing_id}/inventory", {},
            urgent=False,
        )  # fmt: skip
        return _parse("get_listing_inventory", lambda data: _inventory(listing_id, data), body)

    async def list_listings(  # noqa: PLR0913 - the query and its token source
        self,
        shop_id: int,
        *,
        state: ListingState = ListingState.ACTIVE,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[Listing]:
        _check_page(limit, offset)
        params: dict[str, str | int] = {"state": state.value, "limit": limit, "offset": offset}
        body = await self._request(
            "GET",
            "list_listings",
            shop_id,
            f"/shops/{shop_id}/listings",
            params,
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("list_listings", lambda data: _page(data, _listing, offset), body)

    @property
    def config(self) -> EtsyConfig:
        return self._config

    def last_seen_quota(self) -> QuotaSnapshot | None:
        return self._quota

    # --- EtsyShopDirectory (the connect flow, slice 4.3) --------------------------------------

    async def shop_of_user(self, user_id: int, *, access_token: str) -> MarketplaceShop:
        """getShopByOwnerUserId with the token just issued (not yet in the vault). Urgent: the
        owner is waiting for the connection to finish."""
        if user_id <= 0:
            raise ValueError("the Etsy user ID must be positive")
        body = await self._request(
            "GET",
            "shop_of_user",
            user_id,
            f"/users/{user_id}/shops",
            {},
            urgent=True,
            access_token=access_token,
        )
        return _parse("shop_of_user", _shop, body)

    # --- Capability probes (slice 4.5) -------------------------------------------------------

    async def check_receipt_addresses(
        self, shop_id: int, *, urgent: bool = True, tokens: AccessTokenProvider | None = None
    ) -> ReceiptAddressCheck:
        """Read the newest receipt (one, `limit=1`) and report only whether its address fields
        hold anything. No value is kept, returned or logged."""
        body = await self._request(
            "GET",
            "check_receipt_addresses",
            shop_id,
            f"/shops/{shop_id}/receipts",
            {"limit": 1, "offset": 0},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("check_receipt_addresses", _address_check, body)

    # --- Dashboard reads (the shop's live data on the dashboard) -----------------------------

    async def get_shop_profile(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> ShopProfile:
        """getShop with what the shop card shows: counts, reputation and identity."""
        body = await self._request(
            "GET",
            "get_shop_profile",
            shop_id,
            f"/shops/{shop_id}",
            {},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("get_shop_profile", _shop_profile, body)

    async def list_listing_details(
        self,
        shop_id: int,
        *,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[ListingDetails]:
        """getListingsByShop: active listings with their images (`listings_r`)."""
        _check_page(limit, offset)
        params: dict[str, str | int] = {
            "state": ListingState.ACTIVE.value,
            "includes": "Images",
            "limit": limit,
            "offset": offset,
        }
        body = await self._request(
            "GET",
            "list_listing_details",
            shop_id,
            f"/shops/{shop_id}/listings",
            params,
            urgent=urgent,
            tokens=tokens,
        )
        return _parse(
            "list_listing_details", lambda data: _page(data, _listing_details, offset), body
        )

    async def list_sections(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ShopSection, ...]:
        """getShopSections."""
        body = await self._request(
            "GET",
            "list_sections",
            shop_id,
            f"/shops/{shop_id}/sections",
            {},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("list_sections", lambda data: _results(data, _section), body)

    async def list_order_receipts(  # noqa: PLR0913 - the window, the page and the token source
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        min_last_modified: datetime | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
        was_paid: bool | None = None,
        was_shipped: bool | None = None,
    ) -> Page[OrderReceipt]:
        """getShopReceipts created since `min_created` and changed since `min_last_modified`
        (None: no bound), newest first by creation (`transactions_r`), with no status filter.
        Buyer data is minimised here: only a display name and the destination country are kept
        (see `OrderReceipt`)."""
        _check_page(limit, offset)
        params: dict[str, str | int] = {}
        if min_created is not None:
            params["min_created"] = _epoch(min_created, "min_created")
        if min_last_modified is not None:
            params["min_last_modified"] = _epoch(min_last_modified, "min_last_modified")
        # Reconciliation's open orders (slice 5.5): every paid, unshipped receipt, no period.
        if was_paid is not None:
            params["was_paid"] = "true" if was_paid else "false"
        if was_shipped is not None:
            params["was_shipped"] = "true" if was_shipped else "false"
        params |= {
            "sort_on": "created",
            "sort_order": "desc",
            "limit": limit,
            "offset": offset,
        }
        body = await self._request(
            "GET",
            "list_order_receipts",
            shop_id,
            f"/shops/{shop_id}/receipts",
            params,
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("list_order_receipts", lambda data: _page(data, _order_receipt, offset), body)

    async def get_order_receipt(
        self, shop_id: int, receipt_id: int, *, tokens: AccessTokenProvider | None = None
    ) -> OrderReceipt:
        """One receipt for the stored order history (`getShopReceipt`, a GET), parsed like
        `list_order_receipts`: contact data is never read."""
        if receipt_id < 1:
            raise ValueError("receipt_id must be a positive Etsy receipt ID")
        body = await self._request(
            "GET", "get_order_receipt", shop_id, f"/shops/{shop_id}/receipts/{receipt_id}", {},
            urgent=False, tokens=tokens,
        )  # fmt: skip
        return _parse("get_order_receipt", _order_receipt, body)

    # --- The owner-triggered writes (slices 6.6 and 6.8) -------------------------------------

    async def create_receipt_shipment(
        self, shop_id: int, receipt_id: int, *, tracking_code: str, carrier_name: str
    ) -> None:
        """Add tracking to a receipt (`createReceiptShipment`, a POST): Etsy emails the buyer, so
        it is sent once, never retried. The form never reaches a log line or an error."""
        if receipt_id < 1:
            raise ValueError("receipt_id must be a positive Etsy receipt ID")
        if not tracking_code.strip() or not carrier_name.strip():
            raise ValueError("an Etsy shipment needs a tracking code and a carrier")
        await self._request(
            "POST", "create_receipt_shipment", shop_id,
            f"/shops/{shop_id}/receipts/{receipt_id}/tracking", {}, urgent=True,
            allow_write=True,
            form={"tracking_code": tracking_code, "carrier_name": carrier_name},
        )  # fmt: skip

    async def update_shop_vacation(
        self, shop_id: int, *, is_vacation: bool, vacation_message: str | None
    ) -> None:
        """Turn vacation mode on or off (`updateShop`, a PUT): only the vacation fields."""
        fields = {"is_vacation": "true" if is_vacation else "false"}
        if is_vacation and vacation_message:
            fields["vacation_message"] = vacation_message
        await self._request(
            "PUT", "update_shop_vacation", shop_id, f"/shops/{shop_id}", {}, urgent=True,
            allow_write=True, form=fields,
        )  # fmt: skip

    async def list_reviews(
        self,
        shop_id: int,
        *,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[ShopReview]:
        """getReviewsByShop, newest first."""
        _check_page(limit, offset)
        body = await self._request(
            "GET",
            "list_reviews",
            shop_id,
            f"/shops/{shop_id}/reviews",
            {"limit": limit, "offset": offset},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("list_reviews", lambda data: _page(data, _review, offset), body)

    async def list_shipping_profiles(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ShippingProfile, ...]:
        """getShopShippingProfiles (`shops_r`)."""
        body = await self._request(
            "GET",
            "list_shipping_profiles",
            shop_id,
            f"/shops/{shop_id}/shipping-profiles",
            {},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse(
            "list_shipping_profiles", lambda data: _results(data, _shipping_profile), body
        )

    async def list_return_policies(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ReturnPolicy, ...]:
        """getShopReturnPolicies."""
        body = await self._request(
            "GET",
            "list_return_policies",
            shop_id,
            f"/shops/{shop_id}/policies/return",
            {},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse("list_return_policies", lambda data: _results(data, _return_policy), body)

    async def list_production_partners(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ProductionPartner, ...]:
        """getShopProductionPartners (`shops_r`)."""
        body = await self._request(
            "GET",
            "list_production_partners",
            shop_id,
            f"/shops/{shop_id}/production-partners",
            {},
            urgent=urgent,
            tokens=tokens,
        )
        return _parse(
            "list_production_partners", lambda data: _results(data, _production_partner), body
        )

    # --- One call: write guard, floor, throttle, retries ------------------------------------

    async def _request(  # noqa: PLR0913 - the call, its budget class, optional token and flag
        self,
        method: str,
        operation: str,
        shop_id: int,
        path: str,
        params: Mapping[str, str | int],
        *,
        urgent: bool,
        access_token: str | None = None,
        tokens: AccessTokenProvider | None = None,
        allow_write: bool = False,
        form: Mapping[str, str] | None = None,
    ) -> Any:
        """The only way the adapter calls Etsy. A method other than GET is refused here, before
        the budget or a token is touched, unless `allow_write` is set for this one call."""
        verb = method.upper()
        write = verb not in READ_ONLY_METHODS
        if write and not allow_write:
            _refuse_write(verb, path)
        refreshing = self._admit(operation, shop_id, urgent=urgent)
        try:
            return await self._send(
                verb,
                operation,
                shop_id,
                path,
                params,
                access_token=access_token,
                tokens=tokens or self._tokens,
                write=write,
                form=form,
            )
        finally:
            if refreshing:
                self._refresh_in_flight = False

    async def _send(  # noqa: PLR0913 - one request's parts
        self,
        method: str,
        operation: str,
        shop_id: int,
        path: str,
        params: Mapping[str, str | int],
        *,
        access_token: str | None,
        tokens: AccessTokenProvider,
        write: bool,
        form: Mapping[str, str] | None = None,
    ) -> Any:
        url = self._config.base_url.rstrip("/") + path
        # Writes are not idempotent at Etsy: one attempt, never the retry loop.
        max_attempts = 1 if write else self._config.max_attempts
        extensions = {_WRITE_ALLOWED: True} if write else {}
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= max_attempts
            await self._wait_for_slot()
            # Asked again before every attempt, after any wait: once the shop is disconnected the
            # provider refuses, so a call backing off never sends its retry (Codex review of #49).
            token = access_token or await tokens.access_token(shop_id)
            headers = {
                "x-api-key": self._credentials.api_key_header(),
                "authorization": f"Bearer {token}",
                "accept": "application/json",
            }
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=form,
                    headers=headers,
                    timeout=self._timeout,
                    extensions=extensions,
                )
            except httpx.TransportError as error:
                if last:
                    raise MarketplaceUnavailableError(
                        f"Etsy did not answer {operation} ({type(error).__name__})"
                    ) from error
                await self._retry_after_wait(operation, attempt, None, self._backoff(attempt))
                continue
            self._observe_quota(response)
            status = response.status_code
            if response.is_success:
                return _json(operation, response)
            if status == httpx.codes.TOO_MANY_REQUESTS:
                retry_after = _retry_after(response, self._time.now())
                if last or (
                    retry_after is not None and retry_after > self._config.max_retry_after_seconds
                ):
                    raise MarketplaceRateLimitedError(
                        f"Etsy rate-limited {operation}", retry_after_seconds=retry_after
                    )
                wait = retry_after if retry_after is not None else self._backoff(attempt)
                await self._retry_after_wait(operation, attempt, status, wait)
                continue
            if status >= httpx.codes.INTERNAL_SERVER_ERROR:
                if last:
                    raise MarketplaceUnavailableError(
                        f"Etsy answered {status} to {operation}", status=status
                    )
                await self._retry_after_wait(operation, attempt, status, self._backoff(attempt))
                continue
            refused = _client_error(operation, status, _json_or_none(response))
            if isinstance(refused, MarketplaceCredentialsRejectedError) and access_token is None:
                await _tell_rejected(tokens, shop_id, refused.reason)
            raise refused

    def _backoff(self, attempt: int) -> float:
        base = self._config.backoff_base_seconds * 2 ** (attempt - 1)
        return float(min(base, self._config.backoff_max_seconds))

    async def _retry_after_wait(
        self, operation: str, attempt: int, status: int | None, wait: float
    ) -> None:
        _log.info(
            "etsy.retry", operation=operation, attempt=attempt, status=status, wait_seconds=wait
        )
        await self._time.sleep(wait)

    async def _wait_for_slot(self) -> None:
        """Keep at most half the per-second limit in any one-second window."""
        async with self._throttle:
            while True:
                now = self._time.monotonic()
                while self._sent and now - self._sent[0] >= 1.0:
                    self._sent.popleft()
                if len(self._sent) < self._per_second_budget():
                    break
                await self._time.sleep(1.0 - (now - self._sent[0]))
            self._sent.append(self._time.monotonic())

    def _per_second_budget(self) -> int:
        limit = self._config.assumed_limit_per_second
        if self._quota is not None and self._quota.limit_per_second:
            limit = self._quota.limit_per_second
        return max(1, limit // 2)

    # --- Daily budget (AC-4.1.2) --------------------------------------------------------------

    def _observe_quota(self, response: httpx.Response) -> None:
        headers = response.headers
        # An answer without any budget header (a gateway error page, say) says nothing about the
        # budget; it must not erase what Etsy reported last.
        if self._quota is not None and not any(name in headers for name in _QUOTA_HEADERS):
            return
        snapshot = QuotaSnapshot(
            limit_per_second=_count(headers.get("x-limit-per-second")),
            remaining_this_second=_count(headers.get("x-remaining-this-second")),
            limit_per_day=_count(headers.get("x-limit-per-day")),
            remaining_today=_count(headers.get("x-remaining-today")),
            observed_at=self._time.now(),
        )
        self._quota = snapshot
        floor = self._config.daily_quota_floor
        if snapshot.is_below(floor):
            if not self._low_quota_reported:
                self._low_quota_reported = True
                self._low_quota.add(1)
                _log.warning(
                    "etsy.daily_quota_low",
                    remaining_today=snapshot.remaining_today,
                    limit_per_day=snapshot.limit_per_day,
                    floor=floor,
                )
        elif snapshot.remaining_today is not None:
            self._low_quota_reported = False

    def _admit(self, operation: str, shop_id: int, *, urgent: bool) -> bool:
        """Defer a routine call below the floor, or return True if it is the one refresh call.

        Runs without awaiting, so the check and the claim of the refresh slot are atomic for
        concurrent tasks on the event loop.
        """
        snapshot, floor = self._quota, self._config.daily_quota_floor
        if urgent or snapshot is None:
            return False
        remaining = snapshot.remaining_today
        if remaining is None or remaining >= floor:
            return False
        stale = self._time.now() - snapshot.observed_at >= self._config.quota_snapshot_ttl
        if stale and not self._refresh_in_flight:
            self._refresh_in_flight = True
            return True
        self._deferred_calls.add(1, {"operation": operation})
        _log.warning(
            "etsy.call_deferred",
            operation=operation,
            shop_id=shop_id,
            remaining_today=remaining,
            floor=floor,
        )
        raise QuotaDeferredError(
            f"Etsy {operation} deferred: the daily request budget is below the floor",
            remaining_today=remaining,
            floor=floor,
        )


# --- The write guard ---------------------------------------------------------------------------


def _refuse_write(method: str, path: str) -> NoReturn:
    # The path names the operation (shop and receipt IDs, no buyer data); never the body.
    _log.warning("etsy.write_refused", method=method, path=path)
    raise MarketplaceWriteRefusedError(f"HTCO does not write to Etsy: {method} refused")


def _install_write_guard(client: httpx.AsyncClient, base_url: str) -> None:
    """Add (once per client and base URL) a request hook that refuses non-GET shop API calls."""
    hooks = client.event_hooks
    requests = list(hooks.get("request", []))
    if any(getattr(hook, "etsy_base_url", None) == base_url for hook in requests):
        return
    hooks["request"] = [*requests, _WriteGuard(base_url)]
    client.event_hooks = hooks


class _WriteGuard:
    """httpx request hook: the second guard, below the adapter, on the client itself."""

    def __init__(self, base_url: str) -> None:
        self.etsy_base_url = base_url
        self._root = base_url.rstrip("/")

    async def __call__(self, request: httpx.Request) -> None:
        method = request.method.upper()
        if method in READ_ONLY_METHODS or request.extensions.get(_WRITE_ALLOWED) is True:
            return
        host = (request.url.host or "").lower().rstrip(".")
        if request.url.scheme == "https" and (method, host, request.url.path) == _TOKEN_EXCHANGE:
            return  # https only (sage's hardening, #126)
        url = str(request.url)
        if (
            host == "etsy.com"
            or host.endswith(".etsy.com")
            or url == self._root
            or url.startswith(self._root + "/")
            or url.startswith(self._root + "?")
        ):
            _refuse_write(method, request.url.path)


# --- HTTP helpers -----------------------------------------------------------------------------


async def _tell_rejected(tokens: AccessTokenProvider, shop_id: int, reason: str) -> None:
    """Let the token source mark the connection `needs_reauth`; a failure there never hides
    Etsy's answer."""
    if not isinstance(tokens, CredentialsRejectionSink):
        return
    try:
        await tokens.credentials_rejected(shop_id, reason=reason)
    except Exception as failure:
        _log.warning("etsy.rejection_not_recorded", error=type(failure).__name__)


# Etsy's answer for a key it does not accept ("Invalid API key", an `invalid_client`), as
# opposed to a scope or permission the shop did not grant.
_API_KEY_REFUSAL = re.compile(
    r"api[ _-]?key|keystring|invalid_client|shared[ _-]?secret", re.IGNORECASE
)


def _client_error(  # noqa: PLR0911 - one answer per status class
    operation: str, status: int, body: Mapping[str, Any] | None = None
) -> MarketplaceError:
    """Only the body's `error` code and description are looked at, never logged or kept."""
    message = f"Etsy answered {status} to {operation}"
    said = "" if body is None else f"{body.get('error', '')} {body.get('error_description', '')}"
    match status:
        case httpx.codes.UNAUTHORIZED:
            # The provider refreshes before expiry, so a 401 means the token is not valid for
            # this app at all (revoked, or issued to another app's key).
            reason = "api_key_rejected" if _API_KEY_REFUSAL.search(said) else "invalid_token"
            return MarketplaceCredentialsRejectedError(message, status=status, reason=reason)
        case httpx.codes.FORBIDDEN if _API_KEY_REFUSAL.search(said):
            return MarketplaceCredentialsRejectedError(
                message, status=status, reason="api_key_rejected"
            )
        case httpx.codes.FORBIDDEN:
            return MarketplacePermissionError(message, status=status)
        case httpx.codes.NOT_FOUND:
            return MarketplaceNotFoundError(message, status=status)
        case httpx.codes.CONFLICT:
            return MarketplaceConflictError(message, status=status)
        case _ if httpx.codes.BAD_REQUEST <= status < httpx.codes.INTERNAL_SERVER_ERROR:
            return MarketplaceRequestError(message, status=status)
        case _:
            return MarketplaceResponseError(message, status=status)


def _retry_after(response: httpx.Response, now: datetime) -> float | None:
    """Seconds to wait from `retry-after` (delta seconds or an HTTP date); None if unusable."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - now).total_seconds())
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _count(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        number = int(value.strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def _json_or_none(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _json(operation: str, response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise MarketplaceResponseError(
            f"Etsy answered {operation} without JSON", status=response.status_code
        ) from error


def _check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _epoch(moment: datetime, name: str) -> int:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must carry a timezone")
    return int(moment.timestamp())


# --- Payload mapping --------------------------------------------------------------------------


def _parse[ResultT](operation: str, parser: Callable[[Any], ResultT], body: Any) -> ResultT:
    try:
        return parser(body)
    # OverflowError and OSError: a timestamp outside the platform's datetime range.
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
        raise MarketplaceResponseError(
            f"Etsy answered {operation} with an unexpected payload"
        ) from error


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _optional_int(data: Mapping[str, Any], key: str) -> int | None:
    return None if data.get(key) is None else _int(data, key)


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    return None if data.get(key) is None else _str(data, key)


def _bool(data: Mapping[str, Any], key: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _timestamp(data: Mapping[str, Any], key: str) -> datetime:
    return datetime.fromtimestamp(_int(data, key), UTC)


def _money(value: object) -> MarketplaceMoney:
    data = _object(value)
    return MarketplaceMoney(
        amount=_int(data, "amount"),
        divisor=_int(data, "divisor"),
        currency_code=_str(data, "currency_code"),
    )


def _page[ItemT](body: object, item: Callable[[Any], ItemT], offset: int) -> Page[ItemT]:
    data = _object(body)
    results = data["results"]
    if not isinstance(results, list):
        raise TypeError("results must be a list")
    return Page(
        items=tuple(item(entry) for entry in results),
        total_count=_int(data, "count"),
        offset=offset,
    )


def _shop(body: object) -> MarketplaceShop:
    data = _object(body)
    return MarketplaceShop(
        shop_id=_int(data, "shop_id"),
        name=_str(data, "shop_name"),
        currency_code=_str(data, "currency_code"),
        url=_optional_str(data, "url"),
        is_vacation=_bool(data, "is_vacation"),
        active_listing_count=_int(data, "listing_active_count"),
        sold_transaction_count=_int(data, "transaction_sold_count"),
        created_at=_timestamp(data, "created_timestamp"),
        updated_at=_timestamp(data, "updated_timestamp"),
    )


def _address_check(body: object) -> ReceiptAddressCheck:
    data = _object(body)
    results = data["results"]
    if not isinstance(results, list):
        raise TypeError("results must be a list")
    if not results:
        return ReceiptAddressCheck(receipts_seen=0, address_present=None)
    receipt = _object(results[0])
    present = True
    for name in _ADDRESS_FIELDS:
        value = receipt.get(name)
        present = present and isinstance(value, str) and bool(value.strip())
    return ReceiptAddressCheck(receipts_seen=1, address_present=present)


def _receipt_line(value: object) -> ReceiptLine:
    data = _object(value)
    return ReceiptLine(
        transaction_id=_int(data, "transaction_id"),
        listing_id=_optional_int(data, "listing_id"),
        sku=_optional_str(data, "sku"),
        quantity=_int(data, "quantity"),
        unit_price=_money(data["price"]),
    )


def _receipt(value: object) -> Receipt:
    data = _object(value)
    transactions = data.get("transactions") or []
    if not isinstance(transactions, list):
        raise TypeError("transactions must be a list")
    return Receipt(
        receipt_id=_int(data, "receipt_id"),
        status=_str(data, "status").lower(),
        is_paid=_bool(data, "is_paid"),
        is_shipped=_bool(data, "is_shipped"),
        created_at=_timestamp(data, "created_timestamp"),
        updated_at=_timestamp(data, "updated_timestamp"),
        grand_total=_money(data["grandtotal"]),
        lines=tuple(_receipt_line(entry) for entry in transactions),
    )


def _listing_snapshot(value: object) -> ListingSnapshot:
    data = _object(value)
    return ListingSnapshot(
        listing_id=_int(data, "listing_id"),
        etsy_shop_id=_int(data, "shop_id"),
        state=_str(data, "state").lower()[:32],
        quantity=_int(data, "quantity"),
        updated_at=_timestamp(data, "updated_timestamp"),
    )


def _inventory(listing_id: int, value: object) -> InventorySnapshot:
    """Each product's stock: the quantities of its enabled, not deleted offerings."""
    data = _object(value)
    products = data.get("products") or []
    if not isinstance(products, list):
        raise TypeError("products must be a list")
    stock = []
    for raw in products:
        product = _object(raw)
        if product.get("is_deleted") is True:
            continue
        offerings = product.get("offerings") or []
        if not isinstance(offerings, list):
            raise TypeError("offerings must be a list")
        quantity = sum(
            _int(_object(o), "quantity")
            for o in offerings
            if _object(o).get("is_enabled", True) is not False
            and _object(o).get("is_deleted") is not True
        )
        stock.append(ProductStock(product_id=_int(product, "product_id"), quantity=quantity))
    return InventorySnapshot(listing_id=listing_id, products=tuple(stock))


def _listing(value: object) -> Listing:
    data = _object(value)
    return Listing(
        listing_id=_int(data, "listing_id"),
        title=_str(data, "title"),
        state=ListingState(_str(data, "state")),
        quantity=_int(data, "quantity"),
        price=_money(data["price"]),
        url=_optional_str(data, "url"),
        updated_at=_timestamp(data, "updated_timestamp"),
    )


# --- Dashboard payloads ---------------------------------------------------------------------------


def _results[ItemT](body: object, item: Callable[[Any], ItemT]) -> tuple[ItemT, ...]:
    data = _object(body)
    results = data["results"]
    if not isinstance(results, list):
        raise TypeError("results must be a list")
    return tuple(item(entry) for entry in results)


def _list_of(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    return value


def _strings(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = _list_of(data, key)
    if not all(isinstance(v, str) for v in value):
        raise TypeError(f"{key} must be a list of strings")
    return tuple(value)


def _optional_bool(data: Mapping[str, Any], key: str) -> bool:
    return False if data.get(key) is None else _bool(data, key)


def _optional_timestamp(data: Mapping[str, Any], key: str) -> datetime | None:
    return None if data.get(key) is None else _timestamp(data, key)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a number")
    return Decimal(str(value))


def _text(data: Mapping[str, Any], key: str) -> str | None:
    """An optional string; blank counts as missing."""
    value = _optional_str(data, key)
    return value if value is not None and value.strip() else None


def _shop_profile(body: object) -> ShopProfile:
    data = _object(body)
    return ShopProfile(
        shop_id=_int(data, "shop_id"),
        name=_str(data, "shop_name"),
        title=_text(data, "title"),
        url=_text(data, "url"),
        currency_code=_str(data, "currency_code"),
        icon_url=_text(data, "icon_url_fullxfull"),
        banner_url=_text(data, "image_url_760x100"),
        is_vacation=_bool(data, "is_vacation"),
        active_listing_count=_int(data, "listing_active_count"),
        digital_listing_count=_optional_int(data, "digital_listing_count") or 0,
        sold_transaction_count=_int(data, "transaction_sold_count"),
        favorer_count=_optional_int(data, "num_favorers") or 0,
        review_average=_decimal(data.get("review_average")),
        review_count=_optional_int(data, "review_count"),
        created_at=_timestamp(data, "created_timestamp"),
        ships_from_country=_text(data, "shipping_from_country_iso"),
        location_country=_text(data, "shop_location_country_iso"),
        languages=_strings(data, "languages"),
        accepts_custom_requests=_optional_bool(data, "accepts_custom_requests"),
    )


def _first_image(data: Mapping[str, Any]) -> ListingImage | None:
    images = sorted(
        (_object(image) for image in _list_of(data, "images")),
        key=lambda image: _int(image, "rank"),
    )
    if not images:
        return None
    image = images[0]
    return ListingImage(
        thumbnail_url=_str(image, "url_170x135"),
        url=_str(image, "url_570xN"),
        alt_text=_text(image, "alt_text"),
    )


def _listing_details(value: object) -> ListingDetails:
    data = _object(value)
    return ListingDetails(
        listing_id=_int(data, "listing_id"),
        title=_str(data, "title"),
        state=ListingState(_str(data, "state")),
        quantity=_int(data, "quantity"),
        price=_money(data["price"]),
        url=_text(data, "url"),
        image=_first_image(data),
        views=_optional_int(data, "views") or 0,
        favorer_count=_optional_int(data, "num_favorers") or 0,
        tags=_strings(data, "tags"),
        section_id=_optional_int(data, "shop_section_id"),
        is_digital=_optional_str(data, "listing_type") == "download",
        is_personalizable=_optional_bool(data, "is_personalizable"),
        has_variations=_optional_bool(data, "has_variations"),
        created_at=_timestamp(data, "created_timestamp"),
        updated_at=_timestamp(data, "updated_timestamp"),
    )


def _section(value: object) -> ShopSection:
    data = _object(value)
    return ShopSection(
        section_id=_int(data, "shop_section_id"),
        title=_str(data, "title"),
        rank=_int(data, "rank"),
        active_listing_count=_int(data, "active_listing_count"),
    )


def _full_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _shipping_address(data: Mapping[str, Any]) -> ShippingAddress | None:
    """The address fields Etsy shares with the seller; None when it shares none of them."""
    address = ShippingAddress(
        first_line=_text(data, "first_line"),
        second_line=_text(data, "second_line"),
        city=_text(data, "city"),
        state=_text(data, "state"),
        postal_code=_text(data, "zip"),
        country_code=_text(data, "country_iso"),
    )
    parts = (
        address.first_line,
        address.second_line,
        address.city,
        address.state,
        address.postal_code,
    )
    return None if all(part is None for part in parts) else address


def _order_line(value: object) -> OrderLine:
    data = _object(value)
    return OrderLine(
        transaction_id=_int(data, "transaction_id"),
        listing_id=_optional_int(data, "listing_id"),
        title=_optional_str(data, "title") or "",
        sku=_text(data, "sku"),
        quantity=_int(data, "quantity"),
        unit_price=_money(data["price"]),
        is_digital=_optional_bool(data, "is_digital"),
        expected_ship_date=_optional_timestamp(data, "expected_ship_date"),
    )


def _shipment(value: object) -> OrderShipment:
    data = _object(value)
    return OrderShipment(
        carrier=_optional_str(data, "carrier_name") or "",
        tracking_code=_optional_str(data, "tracking_code") or "",
        notified_at=_timestamp(data, "shipment_notification_timestamp"),
    )


def _refund(value: object) -> OrderRefund:
    data = _object(value)
    return OrderRefund(
        amount=_money(data["amount"]),
        created_at=_timestamp(data, "created_timestamp"),
        reason=_text(data, "reason"),
        status=_text(data, "status"),
        note_from_issuer=_text(data, "note_from_issuer"),
    )


def _order_receipt(value: object) -> OrderReceipt:
    """Reads what the Orders screen shows and the order history stores. The buyer's email, phone,
    messages, gift message and notes are never read; the name and the shipping address are kept
    out of every repr (see `OrderReceipt`)."""
    data = _object(value)
    name = _full_name(data.get("name"))
    return OrderReceipt(
        receipt_id=_int(data, "receipt_id"),
        status=_str(data, "status").lower(),
        is_paid=_bool(data, "is_paid"),
        is_shipped=_bool(data, "is_shipped"),
        created_at=_timestamp(data, "created_timestamp"),
        updated_at=_timestamp(data, "updated_timestamp"),
        grand_total=_money(data["grandtotal"]),
        subtotal=_money(data["subtotal"]),
        shipping_total=_money(data["total_shipping_cost"]),
        discount_total=_money(data["discount_amt"]),
        lines=tuple(_order_line(entry) for entry in _list_of(data, "transactions")),
        shipments=tuple(_shipment(entry) for entry in _list_of(data, "shipments")),
        buyer_display_name=display_name(name),
        country_code=_text(data, "country_iso"),
        buyer_name=name,
        shipping_address=_shipping_address(data),
        refunds=tuple(
            sorted(
                (_refund(entry) for entry in _list_of(data, "refunds")),
                key=lambda refund: refund.created_at,
            )
        ),
    )


_STARS = range(1, 6)


def _review(value: object) -> ShopReview:
    data = _object(value)
    rating = _int(data, "rating")
    if rating not in _STARS:
        raise ValueError("rating must be between 1 and 5")
    return ShopReview(
        listing_id=_int(data, "listing_id"),
        rating=rating,
        language=_text(data, "language"),
        created_at=_timestamp(data, "created_timestamp"),
        text=_text(data, "review"),
    )


def _destination(value: object) -> ShippingDestination:
    data = _object(value)
    region = _text(data, "destination_region")
    return ShippingDestination(
        country_code=_text(data, "destination_country_iso"),
        region=None if region == "none" else region,
        first_item_cost=_money(data["primary_cost"]),
        additional_item_cost=_money(data["secondary_cost"]),
        min_delivery_days=_optional_int(data, "min_delivery_days"),
        max_delivery_days=_optional_int(data, "max_delivery_days"),
        mail_class=_text(data, "mail_class"),
    )


def _shipping_profile(value: object) -> ShippingProfile:
    data = _object(value)
    return ShippingProfile(
        profile_id=_int(data, "shipping_profile_id"),
        title=_text(data, "title"),
        origin_country=_str(data, "origin_country_iso"),
        profile_type=_text(data, "profile_type") or "manual",
        destinations=tuple(
            _destination(entry) for entry in _list_of(data, "shipping_profile_destinations")
        ),
        upgrade_count=len(_list_of(data, "shipping_profile_upgrades")),
    )


def _return_policy(value: object) -> ReturnPolicy:
    data = _object(value)
    return ReturnPolicy(
        policy_id=_int(data, "return_policy_id"),
        accepts_returns=_bool(data, "accepts_returns"),
        accepts_exchanges=_bool(data, "accepts_exchanges"),
        return_deadline_days=_optional_int(data, "return_deadline"),
    )


def _production_partner(value: object) -> ProductionPartner:
    data = _object(value)
    return ProductionPartner(
        partner_id=_int(data, "production_partner_id"),
        name=_str(data, "partner_name"),
        location=_optional_str(data, "location") or "",
    )
