"""Accepting an invitation link in the web app: set a password, get a web session (slice 2.6/3.5).

The invitation link lands on the API's link callback with a one-time token, which the API redeems
on the server and keeps sealed in an `HttpOnly` cookie (Codex security review, owner gate v4); the
web app posts only the chosen password and gets the same cookie as a sign-in, never a token.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.sealed_cookies import LINK_COOKIE
from htco_api.web_auth import SESSION_COOKIE, WebAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_application.web_sessions import MAX_FAILURES_PER_IP
from htco_domain.identity import Role
from htco_platform.crypto import Sealer
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.session_fakes import INVITE_ACCESS, NEW_PASSWORD, NOW, USER, Auth, Store, Tokens

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
PROBLEMS = "https://ht-co.app/problems/"
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
CSRF = {"X-HTCO-CSRF": "1", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
ACCEPT = "/v1/auth/invitations/accept"
LINK = "/v1/auth/links/callback"
ACCEPT_PAGE = "https://dash.ht-co.app/accept-invite"


class Memberships:
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return [Membership(ORG, Role.ADMIN, frozenset())] if user.user_id == USER else []


@pytest.fixture
def auth() -> Auth:
    return Auth()


@pytest.fixture
def body() -> dict[str, str]:
    return {"password": NEW_PASSWORD}


async def _open_link(
    http: httpx.AsyncClient,
    token_hash: str = "invite-hash",  # noqa: S107 - a fake link token
) -> httpx.Response:
    return await http.get(LINK, params={"token_hash": token_hash, "type": "invite"})


@pytest.fixture
async def client(auth: Auth) -> AsyncIterator[httpx.AsyncClient]:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    web = WebAuth(
        auth=auth,
        tokens=Tokens(),
        store=Store(),
        allowed_origins=(ORIGIN,),
        link_pages={"invite": ACCEPT_PAGE},
        link_sealer=Sealer(b"k" * 32),
    )
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships()), web_auth=web, clock=lambda: NOW
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        yield http


async def test_accept__sets_the_password_and_signs_in_like_sign_in(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str]
) -> None:
    opened = await _open_link(client)
    assert opened.status_code == 303
    assert opened.headers["location"] == ACCEPT_PAGE
    assert opened.headers["set-cookie"].startswith(f"{LINK_COOKIE}=")
    assert INVITE_ACCESS not in opened.headers["set-cookie"]

    response = await client.post(ACCEPT, json=body, headers=CSRF)

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER), "assurance": "aal1"}
    assert auth.passwords_set == [(INVITE_ACCESS, NEW_PASSWORD)]
    assert "access" not in response.text
    assert "refresh" not in response.text
    [cookie] = [
        c for c in response.headers.get_list("set-cookie") if c.startswith(f"{SESSION_COOKIE}=")
    ]
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    assert response.headers["cache-control"] == "no-store"
    me = await client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == str(USER)


async def test_accept__invalid_link__to_the_page_with_link_invalid_then_401(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    opened = await _open_link(client, "forged-hash")
    response = await client.post(ACCEPT, json=body, headers=CSRF)

    assert opened.headers["location"] == f"{ACCEPT_PAGE}?link=invalid"
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "invitation-refused"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


async def test_accept__a_reset_links_cookie__401(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str]
) -> None:
    # A recovery link has no page here: the callback answers 503 and sets nothing.
    opened = await client.get(LINK, params={"token_hash": "recovery-hash", "type": "recovery"})
    response = await client.post(ACCEPT, json=body, headers=CSRF)

    assert opened.status_code == 503
    assert response.status_code == 401
    assert auth.passwords_set == []


async def test_accept__weak_password__422_weak_password(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    await _open_link(client)
    response = await client.post(
        ACCEPT, json=body | {"password": "weak but long enough"}, headers=CSRF
    )
    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "weak-password"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "password", ["eleven char", "x" * 129], ids=["shorter-than-12", "longer-than-128"]
)
async def test_accept__password_length__422_validation(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str], password: str
) -> None:
    await _open_link(client)
    response = await client.post(ACCEPT, json=body | {"password": password}, headers=CSRF)
    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"
    assert auth.passwords_set == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        {"X-HTCO-CSRF": "1", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    ],
    ids=["no-header", "cross-site-origin"],
)
async def test_accept__fails_csrf_checks__403(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str], headers: dict[str, str]
) -> None:
    await _open_link(client)
    response = await client.post(ACCEPT, json=body, headers=headers)
    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert auth.passwords_set == []


async def test_accept__repeated_failed_links__to_the_page_with_link_too_many(
    client: httpx.AsyncClient,
) -> None:
    for n in range(MAX_FAILURES_PER_IP):
        await _open_link(client, f"invalid-{n}")
    response = await _open_link(client)
    assert response.headers["location"] == f"{ACCEPT_PAGE}?link=too-many"


async def test_accept__not_configured__503(body: dict[str, str]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="https://api.test"
    ) as http:
        response = await http.post(ACCEPT, json=body, headers=CSRF)
    assert response.status_code == 503
