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
from htco_api.main import _Inviter, main
from htco_api.settings import ApiSettings
from htco_application.access.management import InviteeAlreadyRegisteredError
from htco_platform.http import new_async_http_client
from htco_platform.supabase_admin import SupabaseInviter

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
    subject, client = inviter(httpx.Response(200, json={"id": str(NEW_USER)}))
    async with client:
        assert await subject.invite("n@x.test") == NEW_USER


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


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("secret", "on"), [("sb_secret_x", True), (None, False)])
def test_main__invitations_need_the_secret_key(
    environment: pytest.MonkeyPatch, secret: str | None, *, on: bool
) -> None:
    if secret is not None:
        environment.setenv("HTCO_SUPABASE_SECRET_KEY", secret)
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
