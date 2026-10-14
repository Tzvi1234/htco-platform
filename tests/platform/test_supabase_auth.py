"""The calls the BFF makes to Supabase Auth (slice 3.5): password sign-in, refresh, sign-out."""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_platform.http import new_async_http_client
from htco_platform.supabase_auth import (
    AuthServiceError,
    AuthTokens,
    InvalidCredentialsError,
    RateLimitedError,
    SupabaseAuth,
)

pytestmark = pytest.mark.anyio

URL = "https://abc.supabase.co"
KEY = "sb_publishable_test"
TOKENS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_at": 1_800_000_000,
    "expires_in": 3600,
    "token_type": "bearer",
    "user": {"id": "11111111-0000-0000-0000-000000000001"},
}


def auth(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[SupabaseAuth, httpx.AsyncClient]:
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(handler)
    )
    return SupabaseAuth(client, supabase_url=URL, publishable_key=KEY), client


async def test_sign_in__posts_the_password_grant_and_returns_the_tokens() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TOKENS)

    subject, client = auth(handler)
    async with client:
        tokens = await subject.sign_in_with_password("owner@example.test", "correct horse")

    assert tokens == AuthTokens(
        access_token="access-1",  # noqa: S106 - test value
        refresh_token="refresh-1",  # noqa: S106 - test value
        expires_at=datetime.fromtimestamp(1_800_000_000, UTC),
    )
    [request] = seen
    assert str(request.url) == f"{URL}/auth/v1/token?grant_type=password"
    assert request.headers["apikey"] == KEY
    assert json.loads(request.content) == {
        "email": "owner@example.test",
        "password": "correct horse",
    }


async def test_refresh__posts_the_refresh_grant() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TOKENS | {"refresh_token": "refresh-2"})

    subject, client = auth(handler)
    async with client:
        tokens = await subject.refresh("refresh-1")

    assert tokens.refresh_token == "refresh-2"  # noqa: S105 - test value
    assert str(seen[0].url) == f"{URL}/auth/v1/token?grant_type=refresh_token"
    assert json.loads(seen[0].content) == {"refresh_token": "refresh-1"}


async def test_tokens_without_expires_at__use_expires_in() -> None:
    body = {k: v for k, v in TOKENS.items() if k != "expires_at"}
    subject, client = auth(lambda _: httpx.Response(200, json=body))
    async with client:
        before = datetime.now(UTC)
        tokens = await subject.sign_in_with_password("a@b.c", "x")
    assert 3590 <= (tokens.expires_at - before).total_seconds() <= 3610


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error_code": "invalid_credentials"}),
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(400, json={"error_code": "refresh_token_not_found"}),
    ],
)
async def test_rejected_credentials__are_invalid_credentials(response: httpx.Response) -> None:
    subject, client = auth(lambda _: response)
    async with client:
        with pytest.raises(InvalidCredentialsError):
            await subject.sign_in_with_password("a@b.c", "wrong")


async def test_too_many_requests__is_rate_limited() -> None:
    subject, client = auth(
        lambda _: httpx.Response(429, json={"error_code": "over_request_rate_limit"})
    )
    async with client:
        with pytest.raises(RateLimitedError):
            await subject.sign_in_with_password("a@b.c", "x")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"no": "tokens"}),
        httpx.Response(200, content=b"x"),
    ],
    ids=["server-error", "no-tokens", "not-json"],
)
async def test_service_failures__are_auth_service_errors(response: httpx.Response) -> None:
    subject, client = auth(lambda _: response)
    async with client:
        with pytest.raises(AuthServiceError):
            await subject.refresh("r")


async def test_unreachable__is_an_auth_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    subject, client = auth(handler)
    async with client:
        with pytest.raises(AuthServiceError):
            await subject.sign_in_with_password("a@b.c", "x")


async def test_sign_out__revokes_this_session_only_and_ignores_already_gone() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)  # the access token already expired: nothing left to revoke

    subject, client = auth(handler)
    async with client:
        await subject.sign_out("access-1")

    assert str(seen[0].url) == f"{URL}/auth/v1/logout?scope=local"
    assert seen[0].headers["authorization"] == "Bearer access-1"


async def test_tokens__repr_hides_them() -> None:
    tokens = AuthTokens("secret-access", "secret-refresh", datetime.now(UTC))
    assert "secret" not in repr(tokens)
