"""MFA, step-up and the session list through the API (slice 3.5 part 2)."""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Annotated, override

import httpx
import pytest
from fastapi import Depends

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.policy_guard import require_capability
from htco_api.web_auth import WebAuth
from htco_application.access.authentication import AuthContext, AuthenticatedUser, Membership
from htco_application.access.policy import Capability
from htco_application.mfa import Enrollment, Factor
from htco_application.web_sessions import CredentialsRefusedError, IssuedTokens
from htco_domain.identity import AssuranceLevel, Role
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.session_fakes import NOW, USER, Auth, Store, Tokens, tokens

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
PROBLEMS = "https://ht-co.app/problems/"
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
CSRF = {"X-HTCO-CSRF": "1", "Origin": ORIGIN}
CREDENTIALS = {"email": "owner@example.test", "password": "correct horse battery"}


class Mfa:
    def __init__(self) -> None:
        self.factors: list[Factor] = []

    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> Enrollment:  # noqa: ARG002
        self.factors.append(Factor("f1", verified=False, name=friendly_name))
        return Enrollment("f1", "data:image/svg+xml;utf-8,<svg/>", "SECRET", "otpauth://totp/x")

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> IssuedTokens:
        assert access_token
        assert factor_id == "f1"
        if code != "123456":
            raise CredentialsRefusedError
        self.factors = [replace(f, verified=True) for f in self.factors]
        return tokens(50)

    async def factors_of(self, access_token: str) -> list[Factor]:  # noqa: ARG002 - fake
        return list(self.factors)


class Aal2AfterVerify(Tokens):
    """Token 50 is the one Supabase issues after MFA: it carries `aal2`."""

    @override
    async def read(self, access_token: str) -> AuthenticatedUser:
        aal2 = access_token == "access-50"  # noqa: S105 - test value
        aal = AssuranceLevel.AAL2 if aal2 else AssuranceLevel.AAL1
        return AuthenticatedUser(user_id=USER, assurance=aal, session_id=None, claims={})


class Memberships:
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:  # noqa: ARG002
        return [Membership(ORG, Role.OWNER, frozenset())]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
async def client(store: Store, clock: Clock) -> AsyncIterator[httpx.AsyncClient]:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    web = WebAuth(
        auth=Auth(), tokens=Aal2AfterVerify(), store=store, allowed_origins=(ORIGIN,), mfa=Mfa()
    )
    app = create_app(
        AppOptions(authenticator=Authenticator(verifier, Memberships()), web_auth=web, clock=clock)
    )

    @app.post("/test/connect-shop")
    async def connect_shop(
        auth: Annotated[AuthContext, Depends(require_capability(Capability.CONNECT_SHOP, ORG))],
    ) -> dict[str, str]:
        return {"ok": str(auth.user_id)}

    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        assert (
            await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)
        ).status_code == 200
        yield http


async def test_enroll_then_verify__session_becomes_aal2(client: httpx.AsyncClient) -> None:
    before = (await client.get("/v1/auth/mfa")).json()
    enrollment = await client.post("/v1/auth/mfa/enroll", json={"name": "Phone"}, headers=CSRF)
    verified = await client.post(
        "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "123456"}, headers=CSRF
    )
    after = (await client.get("/v1/auth/mfa")).json()

    assert before == {"assurance": "aal1", "enrolled": False, "factors": []}
    assert enrollment.status_code == 200
    assert enrollment.json()["qr_code"].startswith("data:image/svg+xml")
    assert enrollment.headers["cache-control"] == "no-store"
    assert verified.status_code == 200
    assert verified.json() == {"assurance": "aal2"}
    assert after["assurance"] == "aal2"
    assert after["enrolled"] is True


async def test_wrong_code__401_mfa_code_refused(client: httpx.AsyncClient) -> None:
    await client.post("/v1/auth/mfa/enroll", json={"name": "Phone"}, headers=CSRF)
    response = await client.post(
        "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "000000"}, headers=CSRF
    )
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "mfa-code-refused"


async def test_step_up_required__until_the_code_is_reentered_and_for_ten_minutes(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    """AC-3.5.7, with an injected clock."""
    await client.post("/v1/auth/mfa/enroll", json={"name": "Phone"}, headers=CSRF)
    await client.post(
        "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "123456"}, headers=CSRF
    )

    clock.now = NOW + timedelta(minutes=11)  # the enrollment's elevation has lapsed
    before = await client.post("/test/connect-shop", headers=CSRF)
    stepped = await client.post("/v1/auth/step-up", json={"code": "123456"}, headers=CSRF)
    allowed = await client.post("/test/connect-shop", headers=CSRF)
    clock.now += timedelta(minutes=10)
    lapsed = await client.post("/test/connect-shop", headers=CSRF)

    assert before.status_code == 403
    assert before.json()["type"] == PROBLEMS + "step-up-required"
    assert stepped.status_code == 204
    assert allowed.status_code == 200
    assert lapsed.json()["type"] == PROBLEMS + "step-up-required"


async def test_owner_without_mfa__mfa_required_for_owner_actions(client: httpx.AsyncClient) -> None:
    """AC-3.5.3: the API refuses owner-only calls at aal1."""
    response = await client.post("/test/connect-shop", headers=CSRF)
    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "mfa-required"


async def test_step_up_without_an_authenticator__409(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/step-up", json={"code": "123456"}, headers=CSRF)
    assert response.status_code == 409
    assert response.json()["type"] == PROBLEMS + "mfa-not-enrolled"


async def test_sessions__list_and_revoke_another_device(
    client: httpx.AsyncClient, store: Store
) -> None:
    """AC-3.5.4."""
    other = next(iter(store.sessions.values()))
    other_id = uuid.uuid4()
    store.sessions[other_id] = replace(other, id=other_id, token_hash=b"other-device")

    listed = (await client.get("/v1/auth/sessions")).json()["items"]
    revoked = await client.delete(f"/v1/auth/sessions/{other_id}", headers=CSRF)
    missing = await client.delete(f"/v1/auth/sessions/{uuid.uuid4()}", headers=CSRF)

    assert len(listed) == 2
    assert [item["current"] for item in listed].count(True) == 1
    assert revoked.status_code == 204
    assert store.sessions[other_id].revoked_at is not None
    assert missing.status_code == 404


async def test_mfa_endpoints__need_a_web_session(client: httpx.AsyncClient) -> None:
    client.cookies.clear()
    response = await client.get("/v1/auth/mfa")
    assert response.status_code == 401
