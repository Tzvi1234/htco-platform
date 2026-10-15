"""Accepting an invitation link in the web app: set a password, get a web session (slice 2.6/3.5).

The invitation link lands on the web app with the invited user's Supabase tokens in the URL
fragment; the web app posts them once with the chosen password and gets the same cookie as a
sign-in, never a token.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import SESSION_COOKIE, WebAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_application.web_sessions import MAX_FAILURES_PER_IP
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.session_fakes import NEW_PASSWORD, NOW, USER, Auth, Store, Tokens

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
PROBLEMS = "https://ht-co.app/problems/"
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
CSRF = {"X-HTCO-CSRF": "1", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
ACCEPT = "/v1/auth/invitations/accept"


class Memberships:
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return [Membership(ORG, Role.ADMIN, frozenset())] if user.user_id == USER else []


@pytest.fixture
def auth() -> Auth:
    return Auth()


@pytest.fixture
def body(auth: Auth) -> dict[str, str]:
    access, refresh = auth.invitation()
    return {"access_token": access, "refresh_token": refresh, "password": NEW_PASSWORD}


@pytest.fixture
async def client(auth: Auth) -> AsyncIterator[httpx.AsyncClient]:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    web = WebAuth(auth=auth, tokens=Tokens(), store=Store(), allowed_origins=(ORIGIN,))
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
    response = await client.post(ACCEPT, json=body, headers=CSRF)

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER), "assurance": "aal1"}
    assert auth.passwords_set == [(body["access_token"], NEW_PASSWORD)]
    assert "access" not in response.text
    assert "refresh" not in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    assert response.headers["cache-control"] == "no-store"
    me = await client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == str(USER)


async def test_accept__invalid_link_tokens__401_without_a_cookie(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    response = await client.post(
        ACCEPT, json=body | {"access_token": "invalid-token"}, headers=CSRF
    )
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "invitation-refused"
    assert "set-cookie" not in response.headers


async def test_accept__weak_password__422_weak_password(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
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
    response = await client.post(ACCEPT, json=body, headers=headers)
    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert auth.passwords_set == []


async def test_accept__repeated_failures__429_with_retry_after(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    for n in range(MAX_FAILURES_PER_IP):
        await client.post(ACCEPT, json=body | {"access_token": f"invalid-{n}"}, headers=CSRF)
    response = await client.post(ACCEPT, json=body, headers=CSRF)
    assert response.status_code == 429
    assert response.json()["type"] == PROBLEMS + "too-many-attempts"
    assert int(response.headers["retry-after"]) > 0


async def test_accept__not_configured__503(body: dict[str, str]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="https://api.test"
    ) as http:
        response = await http.post(ACCEPT, json=body, headers=CSRF)
    assert response.status_code == 503
