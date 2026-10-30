"""Inviting users through Supabase Auth's admin API (slice 2.6): a one-time invitation link."""

import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_platform.http import new_async_http_client
from htco_platform.supabase_admin import (
    AlreadyRegisteredError,
    InvitationFailedError,
    Invited,
    SupabaseInviter,
)

pytestmark = pytest.mark.anyio

URL = "https://abc.supabase.co"
NEW_USER = uuid.UUID("11111111-0000-0000-0000-000000000009")
LINK = "https://abc.supabase.co/auth/v1/verify?token=one-time&type=invite"


def inviter(
    handler: Callable[[httpx.Request], httpx.Response], key: str = "sb_secret_abc"
) -> tuple[SupabaseInviter, httpx.AsyncClient]:
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(handler)
    )
    return SupabaseInviter(client, supabase_url=URL, secret_key=key, redirect_to=None), client


async def test_invite__posts_the_email_and_returns_the_new_user_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"id": str(NEW_USER), "email": "n@x.test", "action_link": LINK}
        )

    subject, client = inviter(handler)
    async with client:
        assert (await subject.invite("n@x.test")).user_id == NEW_USER

    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == f"{URL}/auth/v1/admin/generate_link"
    assert json.loads(request.content) == {"type": "invite", "email": "n@x.test"}
    assert request.headers["apikey"] == "sb_secret_abc"
    assert "authorization" not in request.headers


async def test_invite__legacy_service_role_jwt__is_also_sent_as_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": str(NEW_USER), "action_link": LINK})

    subject, client = inviter(handler, key="eyJhbGciOi.legacy.jwt")
    async with client:
        await subject.invite("n@x.test")
    assert seen[0].headers["authorization"] == "Bearer eyJhbGciOi.legacy.jwt"


async def test_invite__redirect_is_passed_when_configured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": str(NEW_USER), "action_link": LINK})

    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(handler)
    )
    subject = SupabaseInviter(
        client,
        supabase_url=URL + "/",
        secret_key="test-key",  # noqa: S106 - a test value
        redirect_to="https://dash.ht-co.app/welcome",
    )
    async with client:
        await subject.invite("n@x.test")
    assert str(seen[0].url) == f"{URL}/auth/v1/admin/generate_link"
    assert json.loads(seen[0].content)["redirect_to"] == "https://dash.ht-co.app/welcome"


@pytest.mark.parametrize("code", ["email_exists", "user_already_exists"])
async def test_invite__existing_account__already_registered(code: str) -> None:
    subject, client = inviter(lambda _: httpx.Response(422, json={"error_code": code}))
    async with client:
        with pytest.raises(AlreadyRegisteredError):
            await subject.invite("n@x.test")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(401, json={"msg": "invalid key"}),
        httpx.Response(422, json={"error_code": "validation_failed"}),
        httpx.Response(200, json={"no": "id", "action_link": LINK}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"id": str(NEW_USER)}),
        httpx.Response(200, json={"id": str(NEW_USER), "action_link": ""}),
        httpx.Response(200, json=[]),
    ],
    ids=[
        "server-error",
        "bad-key",
        "other-422",
        "no-id",
        "not-json",
        "no-link",
        "empty-link",
        "list",
    ],
)
async def test_invite__failures__are_invitation_failed(response: httpx.Response) -> None:
    subject, client = inviter(lambda _: response)
    async with client:
        with pytest.raises(InvitationFailedError):
            await subject.invite("n@x.test")


async def test_invite__unreachable__is_invitation_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    subject, client = inviter(handler)
    async with client:
        with pytest.raises(InvitationFailedError):
            await subject.invite("n@x.test")


async def test_inviter__repr__hides_the_secret_key() -> None:
    subject, client = inviter(lambda _: httpx.Response(200), key="sb_secret_very_private")
    async with client:
        assert "very_private" not in repr(subject)


async def test_invite__returns_the_one_time_link() -> None:
    subject, client = inviter(
        lambda _: httpx.Response(200, json={"id": str(NEW_USER), "action_link": LINK})
    )
    async with client:
        assert await subject.invite("n@x.test") == Invited(NEW_USER, LINK)


async def test_invite__link_under_properties__is_accepted() -> None:
    """supabase-js wraps the link in `properties`; accept that shape as well."""
    body = {"id": str(NEW_USER), "properties": {"action_link": LINK}}
    subject, client = inviter(lambda _: httpx.Response(200, json=body))
    async with client:
        assert await subject.invite("n@x.test") == Invited(NEW_USER, LINK)


async def test_invited__repr__hides_the_link() -> None:
    assert LINK not in repr(Invited(NEW_USER, LINK))


CALLBACK = "https://dash.ht-co.app/api/v1/auth/links/callback"


def callback_inviter(answer: dict[str, object]) -> tuple[SupabaseInviter, httpx.AsyncClient]:
    client = new_async_http_client(
        tracer_provider=TracerProvider(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=answer)),
    )
    subject = SupabaseInviter(
        client,
        supabase_url=URL,
        secret_key="sb_secret_abc",  # noqa: S106 - a test value
        redirect_to=None,
        callback_url=CALLBACK,
    )
    return subject, client


@pytest.mark.parametrize(
    "answer",
    [
        {"id": str(NEW_USER), "action_link": LINK, "hashed_token": "h-1"},
        {"id": str(NEW_USER), "properties": {"action_link": LINK, "hashed_token": "h-1"}},
    ],
    ids=["top-level", "under-properties"],
)
async def test_invite__with_a_callback__the_link_carries_only_the_one_time_token_hash(
    answer: dict[str, object],
) -> None:
    subject, client = callback_inviter(answer)
    async with client:
        invited = await subject.invite("n@x.test")

    # The API's own callback redeems it on the server (Codex security review, owner gate v4);
    # Supabase's action_link would hand the session's tokens to the browser.
    assert invited.link == f"{CALLBACK}?token_hash=h-1&type=invite"


async def test_invite__with_a_callback_but_no_token_hash__invitation_failed() -> None:
    subject, client = callback_inviter({"id": str(NEW_USER), "action_link": LINK})
    async with client:
        with pytest.raises(InvitationFailedError):
            await subject.invite("n@x.test")
