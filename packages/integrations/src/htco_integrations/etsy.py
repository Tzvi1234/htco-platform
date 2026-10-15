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

Retries: only GETs are made here, so 429 (honouring `retry-after`), 5xx and transport failures
(timeouts, refused connections) are retried with capped exponential backoff. Other 4xx answers
map to typed errors at once. Write operations, which are not idempotent at Etsy, must not reuse
this retry loop.

Nothing here logs or raises with the API key, the shared secret, an access token or a payload.
"""

import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import anyio
import httpx
import structlog
from opentelemetry import metrics

from htco_application.marketplace import (
    AccessTokenProvider,
    Listing,
    ListingState,
    MarketplaceAuthError,
    MarketplaceConflictError,
    MarketplaceError,
    MarketplaceMoney,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceRequestError,
    MarketplaceResponseError,
    MarketplaceShop,
    MarketplaceUnavailableError,
    Page,
    QuotaDeferredError,
    QuotaSnapshot,
    Receipt,
    ReceiptLine,
)

ETSY_BASE_URL = "https://openapi.etsy.com/v3/application"
MAX_PAGE_SIZE = 100

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

    # --- MarketplacePort ----------------------------------------------------------------------

    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop:
        body = await self._get("get_shop", shop_id, f"/shops/{shop_id}", {}, urgent=urgent)
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
        body = await self._get(
            "list_receipts", shop_id, f"/shops/{shop_id}/receipts", params, urgent=urgent
        )
        return _parse("list_receipts", lambda data: _page(data, _receipt, offset), body)

    async def list_listings(
        self,
        shop_id: int,
        *,
        state: ListingState = ListingState.ACTIVE,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Listing]:
        _check_page(limit, offset)
        params: dict[str, str | int] = {"state": state.value, "limit": limit, "offset": offset}
        body = await self._get(
            "list_listings", shop_id, f"/shops/{shop_id}/listings", params, urgent=urgent
        )
        return _parse("list_listings", lambda data: _page(data, _listing, offset), body)

    def last_seen_quota(self) -> QuotaSnapshot | None:
        return self._quota

    # --- EtsyShopDirectory (the connect flow, slice 4.3) --------------------------------------

    async def shop_of_user(self, user_id: int, *, access_token: str) -> MarketplaceShop:
        """getShopByOwnerUserId with the token just issued (not yet in the vault). Urgent: the
        owner is waiting for the connection to finish."""
        if user_id <= 0:
            raise ValueError("the Etsy user ID must be positive")
        body = await self._get(
            "shop_of_user",
            user_id,
            f"/users/{user_id}/shops",
            {},
            urgent=True,
            access_token=access_token,
        )
        return _parse("shop_of_user", _shop, body)

    # --- One call: floor, throttle, retries ---------------------------------------------------

    async def _get(  # noqa: PLR0913 - the call, its budget class and an optional token
        self,
        operation: str,
        shop_id: int,
        path: str,
        params: Mapping[str, str | int],
        *,
        urgent: bool,
        access_token: str | None = None,
    ) -> Any:
        refreshing = self._admit(operation, shop_id, urgent=urgent)
        try:
            token = access_token or await self._tokens.access_token(shop_id)
            return await self._send(operation, path, params, token)
        finally:
            if refreshing:
                self._refresh_in_flight = False

    async def _send(
        self, operation: str, path: str, params: Mapping[str, str | int], token: str
    ) -> Any:
        headers = {
            "x-api-key": self._credentials.api_key_header(),
            "authorization": f"Bearer {token}",
            "accept": "application/json",
        }
        url = self._config.base_url.rstrip("/") + path
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= self._config.max_attempts
            await self._wait_for_slot()
            try:
                response = await self._client.get(
                    url, params=params, headers=headers, timeout=self._timeout
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
            raise _client_error(operation, status)

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


# --- HTTP helpers -----------------------------------------------------------------------------


def _client_error(operation: str, status: int) -> MarketplaceError:
    message = f"Etsy answered {status} to {operation}"
    match status:
        case httpx.codes.UNAUTHORIZED:
            return MarketplaceAuthError(message, status=status)
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
