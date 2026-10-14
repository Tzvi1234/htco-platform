"""Authentication in the API (slice 2.3, AC-2.3.1 and AC-2.3.2).

A test-only route depends on `require_auth`; the real endpoints that use it arrive with 2.4.
"""

import time
import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator, CurrentAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_domain.identity import AssuranceLevel, Role
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, USER_ID, FakeJwks, SigningKey, claims
from tests.log_capture import LogCapture

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
KEY = SigningKey("k1")
PROBLEMS = "https://ht-co.app/problems/"


class Memberships:
    def __init__(self, *memberships: Membership) -> None:
        self.memberships = memberships
        self.asked: list[AuthenticatedUser] = []

    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        self.asked.append(user)
        return self.memberships


def build_app(memberships: Memberships, jwks: FakeJwks | None = None) -> FastAPI:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks or FakeJwks(KEY))
    app = create_app(AppOptions(authenticator=Authenticator(verifier, memberships)))

    @app.get("/test/whoami")
    async def whoami(auth: CurrentAuth) -> dict[str, str]:
        return {"user_id": str(auth.user_id), "assurance": auth.assurance.value}

    return app


@pytest.fixture
def memberships() -> Memberships:
    return Memberships(Membership(ORG, Role.SUPPORT, frozenset()))


@pytest.fixture
async def client(memberships: Memberships) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(memberships))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_valid_token_and_membership__reaches_the_route(
    client: httpx.AsyncClient, memberships: Memberships
) -> None:
    response = await client.get("/test/whoami", headers=bearer(KEY.sign(claims(aal="aal2"))))

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER_ID), "assurance": "aal2"}
    assert [asked.user_id for asked in memberships.asked] == [USER_ID]
    assert memberships.asked[0].assurance is AssuranceLevel.AAL2
    assert memberships.asked[0].claims["sub"] == str(USER_ID)


@pytest.mark.parametrize(
    ("headers", "problem"),
    [
        ({}, "unauthenticated"),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, "unauthenticated"),
        ({"Authorization": "Bearer"}, "unauthenticated"),
        (bearer(KEY.sign(claims(exp=int(time.time()) - 5))), "token-expired"),
        (bearer(KEY.sign(claims(aud="other"))), "token-wrong-audience"),
        (bearer(KEY.sign(claims(iss="https://evil.example/auth/v1"))), "token-wrong-issuer"),
        (bearer(SigningKey("k1").sign(claims())), "token-bad-signature"),
        (bearer(KEY.sign(claims(), kid="unknown")), "token-bad-signature"),
        (bearer("not-a-jwt"), "token-invalid"),
        (bearer(KEY.sign(claims(role="anon"))), "token-invalid"),
    ],
    ids=[
        "no-header",
        "basic-scheme",
        "empty-bearer",
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "bad-signature",
        "unknown-key",
        "malformed",
        "anon-key",
    ],
)
async def test_unauthenticated_requests__401_with_a_distinct_problem_type(
    client: httpx.AsyncClient, memberships: Memberships, headers: dict[str, str], problem: str
) -> None:
    """AC-2.3.1."""
    response = await client.get("/test/whoami", headers=headers)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == PROBLEMS + problem
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert memberships.asked == []


async def test_the_five_ac_cases__have_five_different_types(client: httpx.AsyncClient) -> None:
    cases = [
        {},
        bearer(KEY.sign(claims(exp=int(time.time()) - 5))),
        bearer(KEY.sign(claims(aud="other"))),
        bearer(KEY.sign(claims(iss="https://evil.example/auth/v1"))),
        bearer(SigningKey("k1").sign(claims())),
    ]
    types = {(await client.get("/test/whoami", headers=h)).json()["type"] for h in cases}
    assert len(types) == 5


async def test_invalid_token__www_authenticate_says_invalid_token(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/test/whoami", headers=bearer("not-a-jwt"))
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_user_without_membership__403() -> None:
    """AC-2.3.2."""
    transport = httpx.ASGITransport(app=build_app(Memberships()))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/test/whoami", headers=bearer(KEY.sign(claims())))

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "no-membership"


async def test_key_set_unreachable__503_not_401() -> None:
    jwks = FakeJwks(KEY)
    jwks.fail = True
    transport = httpx.ASGITransport(app=build_app(Memberships(), jwks))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/test/whoami", headers=bearer(KEY.sign(claims())))

    assert response.status_code == 503
    assert response.json()["type"] == PROBLEMS + "auth-unavailable"


async def test_no_authenticator_configured__503() -> None:
    app = create_app()

    @app.get("/test/whoami")
    async def whoami(auth: CurrentAuth) -> dict[str, str]:  # pragma: no cover - never reached
        return {"user_id": str(auth.user_id)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/test/whoami", headers=bearer(KEY.sign(claims())))

    assert response.status_code == 503
    assert response.json()["type"] == PROBLEMS + "auth-unavailable"


async def test_rejections__are_logged_with_the_reason_and_never_the_token(
    logs: LogCapture, client: httpx.AsyncClient
) -> None:
    token = KEY.sign(claims(aud="other"))
    await client.get("/test/whoami", headers=bearer(token))

    rejected = [entry for entry in logs.lines() if entry.get("event") == "auth.rejected"]
    assert rejected == [
        {**rejected[0], "event": "auth.rejected", "problem": "token-wrong-audience"}
    ]
    assert token not in logs.raw()


class Resource:
    def __init__(self, events: list[str], name: str) -> None:
        self.events, self.name = events, name

    async def start(self) -> None:
        self.events.append(f"start {self.name}")

    async def stop(self) -> None:
        self.events.append(f"stop {self.name}")


def test_resources__start_with_the_app_and_stop_in_reverse_order() -> None:
    events: list[str] = []
    app = create_app(AppOptions(resources=(Resource(events, "db"), Resource(events, "http"))))
    with TestClient(app):
        assert events == ["start db", "start http"]
    assert events == ["start db", "start http", "stop http", "stop db"]
