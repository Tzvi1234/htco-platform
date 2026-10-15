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
    MfaRequiredError,
    RateLimitedError,
    SamePasswordError,
    SupabaseAuth,
    WeakPasswordError,
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


# MFA (slice 3.5 part 2) -------------------------------------------------------------------------


async def test_enroll_totp__returns_the_factor_and_its_qr_code() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "factor-1",
                "type": "totp",
                "totp": {
                    "qr_code": "data:image/svg+xml;utf-8,<svg/>",
                    "secret": "S3CR3T",
                    "uri": "otpauth://x",
                },
            },
        )

    subject, client = auth(handler)
    async with client:
        enrollment = await subject.enroll_totp("access-1", friendly_name="Phone")

    assert (enrollment.factor_id, enrollment.secret, enrollment.uri) == (
        "factor-1",
        "S3CR3T",
        "otpauth://x",
    )
    assert enrollment.qr_code.startswith("data:image/svg+xml")
    assert str(seen[0].url) == f"{URL}/auth/v1/factors"
    assert seen[0].headers["authorization"] == "Bearer access-1"
    assert json.loads(seen[0].content) == {"factor_type": "totp", "friendly_name": "Phone"}


async def test_verify_totp__challenges_then_verifies_and_returns_aal2_tokens() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json={"id": "challenge-1", "expires_at": 1_800_000_000})
        assert json.loads(request.content) == {"challenge_id": "challenge-1", "code": "123456"}
        return httpx.Response(200, json=TOKENS | {"access_token": "access-aal2"})

    subject, client = auth(handler)
    async with client:
        tokens = await subject.verify_totp("access-1", "factor-1", "123456")

    assert tokens.access_token == "access-aal2"  # noqa: S105 - test value
    assert seen == [
        "POST /auth/v1/factors/factor-1/challenge",
        "POST /auth/v1/factors/factor-1/verify",
    ]


@pytest.mark.parametrize(
    "code", ["mfa_verification_failed", "mfa_challenge_expired", "invalid_credentials"]
)
async def test_verify_totp__wrong_code__is_refused(code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json={"id": "c"})
        return httpx.Response(422, json={"error_code": code})

    subject, client = auth(handler)
    async with client:
        with pytest.raises(InvalidCredentialsError):
            await subject.verify_totp("a", "f", "000000")


async def test_factors__reads_the_users_factors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/user"
        return httpx.Response(
            200,
            json={
                "id": "u",
                "factors": [
                    {
                        "id": "f1",
                        "factor_type": "totp",
                        "status": "verified",
                        "friendly_name": "Phone",
                    },
                    {"id": "f2", "factor_type": "totp", "status": "unverified"},
                ],
            },
        )

    subject, client = auth(handler)
    async with client:
        factors = await subject.factors("access-1")

    assert [(f.id, f.factor_type, f.verified, f.friendly_name) for f in factors] == [
        ("f1", "totp", True, "Phone"),
        ("f2", "totp", False, None),
    ]


async def test_factors__user_without_factors__empty() -> None:
    subject, client = auth(lambda _: httpx.Response(200, json={"id": "u"}))
    async with client:
        assert await subject.factors("a") == []


async def test_unenroll__deletes_the_factor() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "f1"})

    subject, client = auth(handler)
    async with client:
        await subject.unenroll("access-1", "f1")

    assert (seen[0].method, seen[0].url.path) == ("DELETE", "/auth/v1/factors/f1")


# Accepting an invitation: the invited user sets their password --------------------------------


async def test_set_password__updates_the_user_as_themselves() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "11111111-0000-0000-0000-000000000001"})

    subject, client = auth(handler)
    async with client:
        await subject.set_password("access-1", "a long new passphrase")

    [request] = seen
    assert request.method == "PUT"
    assert str(request.url) == f"{URL}/auth/v1/user"
    assert request.headers["authorization"] == "Bearer access-1"
    assert request.headers["apikey"] == KEY
    assert json.loads(request.content) == {"password": "a long new passphrase"}


async def test_set_password__weak__is_a_weak_password_error() -> None:
    body = {"error_code": "weak_password", "msg": "Password should be at least 12 characters."}
    subject, client = auth(lambda _: httpx.Response(422, json=body))
    async with client:
        with pytest.raises(WeakPasswordError):
            await subject.set_password("access-1", "short")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error_code": "bad_jwt"}),
        httpx.Response(403, json={"error_code": "session_not_found"}),
        httpx.Response(404, json={"error_code": "user_not_found"}),
    ],
    ids=["bad-jwt", "session-ended", "user-gone"],
)
async def test_set_password__session_refused__is_invalid_credentials(
    response: httpx.Response,
) -> None:
    subject, client = auth(lambda _: response)
    async with client:
        with pytest.raises(InvalidCredentialsError):
            await subject.set_password("access-1", "a long new passphrase")


async def test_set_password__service_failure__is_an_auth_service_error() -> None:
    subject, client = auth(lambda _: httpx.Response(500))
    async with client:
        with pytest.raises(AuthServiceError):
            await subject.set_password("access-1", "a long new passphrase")


async def test_set_password__authenticator_user_at_aal1__is_an_mfa_required_error() -> None:
    body = {"error_code": "insufficient_aal", "msg": "AAL2 session is required"}
    subject, client = auth(lambda _: httpx.Response(401, json=body))
    async with client:
        with pytest.raises(MfaRequiredError):
            await subject.set_password("access-1", "a long new passphrase")


def test_mfa_required__is_still_an_auth_service_error_for_other_callers() -> None:
    assert issubclass(MfaRequiredError, AuthServiceError)


# Password reset (slice 3.5 part 3) ---------------------------------------------------------------

REDIRECT = "https://dash.ht-co.app/reset-password"


async def test_send_password_reset__posts_the_email_with_the_redirect() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    subject, client = auth(handler)
    async with client:
        await subject.send_password_reset("owner@example.test", REDIRECT)

    [request] = seen
    assert request.method == "POST"
    assert request.url.path == "/auth/v1/recover"
    assert request.url.params["redirect_to"] == REDIRECT
    assert request.headers["apikey"] == KEY
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {"email": "owner@example.test"}


async def test_send_password_reset__rate_limited__is_a_rate_limited_error() -> None:
    body = {"error_code": "over_email_send_rate_limit"}
    subject, client = auth(lambda _: httpx.Response(429, json=body))
    async with client:
        with pytest.raises(RateLimitedError):
            await subject.send_password_reset("owner@example.test", REDIRECT)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error_code": "validation_failed"}),
        httpx.Response(500),
    ],
    ids=["refused", "down"],
)
async def test_send_password_reset__failure__is_an_auth_service_error(
    response: httpx.Response,
) -> None:
    subject, client = auth(lambda _: response)
    async with client:
        with pytest.raises(AuthServiceError) as raised:
            await subject.send_password_reset("owner@example.test", REDIRECT)
    assert "owner@example.test" not in str(raised.value)


async def test_send_password_reset__unreachable__is_an_auth_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    subject, client = auth(handler)
    async with client:
        with pytest.raises(AuthServiceError):
            await subject.send_password_reset("owner@example.test", REDIRECT)


async def test_set_password__the_current_password__is_a_same_password_error() -> None:
    body = {"error_code": "same_password", "msg": "New password should be different"}
    subject, client = auth(lambda _: httpx.Response(422, json=body))
    async with client:
        with pytest.raises(SamePasswordError):
            await subject.set_password("access-1", "a long new passphrase")
