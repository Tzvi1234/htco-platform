"""BFF sign-in for the web app (slice 3.5, AC-3.5.1, AC-3.5.2, AC-3.5.4, AC-3.5.5)."""

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import SESSION_COOKIE, WebAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.session_fakes import NOW, USER, Auth, Store, Tokens

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
PROBLEMS = "https://ht-co.app/problems/"
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
CSRF = {"X-HTCO-CSRF": "1", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
CREDENTIALS = {"email": "owner@example.test", "password": "correct horse battery"}


class Memberships:
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return [Membership(ORG, Role.OWNER, frozenset())] if user.user_id == USER else []


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(store: Store, clock: Clock) -> AsyncIterator[httpx.AsyncClient]:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    web = WebAuth(auth=Auth(), tokens=Tokens(), store=store, allowed_origins=(ORIGIN,))
    app = create_app(
        AppOptions(authenticator=Authenticator(verifier, Memberships()), web_auth=web, clock=clock)
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        yield http


async def sign_in(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)


async def test_sign_in__sets_only_a_host_only_secure_http_only_strict_cookie(
    client: httpx.AsyncClient,
) -> None:
    """AC-3.5.1: the browser gets an opaque cookie and no token."""
    response = await sign_in(client)

    assert response.status_code == 200
    assert response.json()["user_id"] == str(USER)
    assert "access" not in response.text
    assert "refresh" not in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    assert response.headers["cache-control"] == "no-store"


async def test_session_cookie__authenticates_api_requests(client: httpx.AsyncClient) -> None:
    await sign_in(client)

    me = await client.get("/v1/me")
    session = await client.get("/v1/auth/session")

    assert me.status_code == 200
    assert me.json()["user_id"] == str(USER)
    assert session.json()["user_id"] == str(USER)


async def test_wrong_password__401_without_a_cookie(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/sign-in", json=CREDENTIALS | {"password": "nope"}, headers=CSRF
    )
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "sign-in-refused"
    assert "set-cookie" not in response.headers


async def test_repeated_failures__429_with_retry_after(client: httpx.AsyncClient) -> None:
    """AC-3.5.5."""
    for _ in range(5):
        await client.post("/v1/auth/sign-in", json=CREDENTIALS | {"password": "x"}, headers=CSRF)
    response = await sign_in(client)
    assert response.status_code == 429
    assert response.json()["type"] == PROBLEMS + "too-many-attempts"
    assert int(response.headers["retry-after"]) > 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},  # no CSRF header
        {"X-HTCO-CSRF": "1", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        {"X-HTCO-CSRF": "1", "Sec-Fetch-Site": "cross-site"},
    ],
    ids=["no-header", "cross-site-origin", "cross-site-fetch"],
)
async def test_sign_in__fails_csrf_checks__403(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    """AC-3.5.2 (login CSRF)."""
    response = await client.post("/v1/auth/sign-in", json=CREDENTIALS, headers=headers)
    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"


async def test_unsafe_cookie_request__without_csrf_header__403(
    client: httpx.AsyncClient,
) -> None:
    """AC-3.5.2: an unsafe request riding the cookie needs the header and a same-site origin."""
    await sign_in(client)
    without_header = await client.post("/v1/auth/sign-out", headers={"Origin": ORIGIN})
    cross_site = await client.post(
        "/v1/auth/sign-out", headers={"X-HTCO-CSRF": "1", "Origin": "https://evil.example"}
    )
    assert [without_header.status_code, cross_site.status_code] == [403, 403]
    assert (await client.get("/v1/me")).status_code == 200  # still signed in


async def test_sign_out__ends_the_session_and_clears_the_cookie(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    response = await client.post("/v1/auth/sign-out", headers=CSRF)

    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert (await client.get("/v1/me")).status_code == 401


async def test_revoked_elsewhere__next_request_is_401_and_clears_the_cookie(
    client: httpx.AsyncClient, store: Store
) -> None:
    """AC-3.5.4."""
    await sign_in(client)
    [session] = store.sessions.values()
    await store.revoke(session.id, NOW)

    response = await client.get("/v1/me")
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "session-ended"
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_bearer_requests__still_work_and_skip_csrf(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "token-invalid"


async def test_session__without_a_cookie__401(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/auth/session")
    assert response.status_code == 401


async def test_web_sign_in_not_configured__503() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.test"
    ) as http:
        response = await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)
    assert response.status_code == 503
