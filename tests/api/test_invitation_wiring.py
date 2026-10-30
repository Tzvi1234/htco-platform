"""The composition root's inviter: Supabase errors become the API's and application's (2.6)."""

import logging
import os
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from htco_api.access import InvitationFailedError
from htco_api.main import _AuthProvider, _Inviter, main
from htco_api.problems import ProblemError
from htco_api.settings import ApiSettings
from htco_application.access.management import InvitedUser, InviteeAlreadyRegisteredError
from htco_application.web_sessions import (
    CredentialsRefusedError,
    MfaCodeRequiredError,
    ResetEmailNotSentError,
    SamePasswordError,
    TooManyAttemptsError,
    WeakPasswordError,
)
from htco_platform.http import new_async_http_client
from htco_platform.supabase_admin import SupabaseInviter
from htco_platform.supabase_auth import SupabaseAuth

pytestmark = pytest.mark.anyio

NEW_USER = uuid.UUID("11111111-0000-0000-0000-000000000009")


def inviter(response: httpx.Response) -> tuple[_Inviter, httpx.AsyncClient]:
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(lambda _: response)
    )
    supabase = SupabaseInviter(
        client,
        supabase_url="https://abc.supabase.co",
        secret_key="test-key",  # noqa: S106 - a test value
        redirect_to=None,
    )
    return _Inviter(supabase), client


async def test_inviter__success__returns_the_user_id() -> None:
    link = "https://x.test/one-time"
    subject, client = inviter(httpx.Response(200, json={"id": str(NEW_USER), "action_link": link}))
    async with client:
        assert await subject.invite("n@x.test") == InvitedUser(NEW_USER, link)


async def test_inviter__existing_account__is_the_applications_error() -> None:
    subject, client = inviter(httpx.Response(422, json={"error_code": "email_exists"}))
    async with client:
        with pytest.raises(InviteeAlreadyRegisteredError):
            await subject.invite("n@x.test")


async def test_inviter__service_failure__is_the_apis_error() -> None:
    subject, client = inviter(httpx.Response(500))
    async with client:
        with pytest.raises(InvitationFailedError):
            await subject.invite("n@x.test")


def provider(response: httpx.Response) -> tuple[_AuthProvider, httpx.AsyncClient]:
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(lambda _: response)
    )
    supabase = SupabaseAuth(
        client, supabase_url="https://abc.supabase.co", publishable_key="sb_publishable_x"
    )
    return _AuthProvider(supabase), client


async def test_provider__set_password__succeeds() -> None:
    subject, client = provider(httpx.Response(200, json={"id": str(NEW_USER)}))
    async with client:
        await subject.set_password("access-1", "a long new passphrase")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(422, json={"error_code": "weak_password"}), WeakPasswordError),
        (httpx.Response(403, json={"error_code": "session_not_found"}), CredentialsRefusedError),
        (httpx.Response(429), TooManyAttemptsError),
        (httpx.Response(500), ProblemError),
    ],
    ids=["weak", "session-ended", "rate-limited", "down"],
)
async def test_provider__set_password_errors__are_the_applications(
    response: httpx.Response, expected: type[Exception]
) -> None:
    subject, client = provider(response)
    async with client:
        with pytest.raises(expected):
            await subject.set_password("access-1", "a long new passphrase")


async def test_provider__set_password_needing_aal2__asks_for_the_code() -> None:
    subject, client = provider(httpx.Response(401, json={"error_code": "insufficient_aal"}))
    async with client:
        with pytest.raises(MfaCodeRequiredError):
            await subject.set_password("access-1", "a long new passphrase")


async def test_provider__set_password_the_same__is_the_applications() -> None:
    subject, client = provider(httpx.Response(422, json={"error_code": "same_password"}))
    async with client:
        with pytest.raises(SamePasswordError):
            await subject.set_password("access-1", "a long new passphrase")


async def test_provider__send_password_reset__succeeds() -> None:
    subject, client = provider(httpx.Response(200, json={}))
    async with client:
        await subject.send_password_reset("n@x.test", "https://dash.ht-co.app/reset-password")


@pytest.mark.parametrize(
    "response",
    [httpx.Response(429), httpx.Response(400, json={"error_code": "x"}), httpx.Response(500)],
    ids=["rate-limited", "refused", "down"],
)
async def test_provider__send_password_reset_errors__are_not_sent(response: httpx.Response) -> None:
    subject, client = provider(response)
    async with client:
        with pytest.raises(ResetEmailNotSentError):
            await subject.send_password_reset("n@x.test", "https://dash.ht-co.app/reset-password")


class _Server:
    def __init__(self) -> None:
        self.apps: list[FastAPI] = []

    def __call__(self, app: FastAPI, _settings: ApiSettings) -> None:
        self.apps.append(app)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """`main` configures logging for the whole process; put the test runner's handlers back."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_ENVIRONMENT", "local")
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    monkeypatch.setenv("HTCO_SUPABASE_URL", "http://127.0.0.1:54321")
    return monkeypatch


CALLBACK = "https://dash.ht-co.app/api/v1/auth/links/callback"


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    ("secret", "callback", "on"),
    [("sb_secret_x", CALLBACK, True), (None, CALLBACK, False), ("sb_secret_x", None, False)],
    ids=["both", "no-secret", "no-callback"],
)
def test_main__invitations_need_the_secret_key_and_the_link_callback(
    environment: pytest.MonkeyPatch, secret: str | None, callback: str | None, *, on: bool
) -> None:
    if secret is not None:
        environment.setenv("HTCO_SUPABASE_SECRET_KEY", secret)
    if callback is not None:
        environment.setenv("HTCO_AUTH_LINK_CALLBACK_URL", callback)
    server = _Server()

    main(serve=server)

    [app] = server.apps
    assert (app.state.inviter is not None) is on
    assert app.state.access is not None


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("configured", "on"), [(True, True), (False, False)])
def test_main__web_sign_in_needs_the_session_key_and_publishable_key(
    environment: pytest.MonkeyPatch, *, configured: bool, on: bool
) -> None:
    import base64  # noqa: PLC0415 - local to this test

    if configured:
        environment.setenv("HTCO_SESSION_KEY", base64.b64encode(b"k" * 32).decode())
        environment.setenv("HTCO_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_x")
        environment.setenv("HTCO_WEB_ORIGINS", "https://dash.ht-co.app")
    server = _Server()

    main(serve=server)

    [app] = server.apps
    assert (app.state.web_auth is not None) is on


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    "redirect", ["https://dash.ht-co.app/reset-password", None], ids=["set", "unset"]
)
def test_main__password_reset_links_go_to_the_callback_then_the_configured_page(
    environment: pytest.MonkeyPatch, redirect: str | None
) -> None:
    import base64  # noqa: PLC0415 - local to this test

    environment.setenv("HTCO_SESSION_KEY", base64.b64encode(b"k" * 32).decode())
    environment.setenv("HTCO_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_x")
    environment.setenv("HTCO_AUTH_LINK_CALLBACK_URL", CALLBACK)
    if redirect is not None:
        environment.setenv("HTCO_PASSWORD_RESET_REDIRECT_URL", redirect)
    server = _Server()

    main(serve=server)

    [app] = server.apps
    web = app.state.web_auth
    assert web.password_reset_redirect == (None if redirect is None else CALLBACK)
    assert web.link_pages.get("recovery") == redirect
    assert web.link_sealer is not None


async def test_provider__verify_link__returns_the_tokens() -> None:
    subject, client = provider(
        httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    )
    async with client:
        issued = await subject.verify_link("hash-1", "invite")
    assert issued.access_token == "a"  # noqa: S105 - test value


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(403, json={"error_code": "otp_expired"}), CredentialsRefusedError),
        (httpx.Response(429), TooManyAttemptsError),
        (httpx.Response(500), ProblemError),
    ],
    ids=["refused", "rate-limited", "down"],
)
async def test_provider__verify_link_errors__are_the_applications(
    response: httpx.Response, expected: type[Exception]
) -> None:
    subject, client = provider(response)
    async with client:
        with pytest.raises(expected):
            await subject.verify_link("hash-1", "invite")
