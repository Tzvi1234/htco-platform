"""Passkeys through the API (issue #113): add one from an `aal2` session, confirm the sign-in's
second step with it (or fall back to a code), remove a factor but never the last app, refuse a
credential from another origin, audit the changes, and stay off unless switched on."""

import base64
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import WebAuth
from htco_application.mfa import Factor
from htco_application.passkeys import PASSKEY, Ceremony, PasskeyChallenge
from htco_application.web_sessions import CredentialsRefusedError, IssuedTokens
from htco_platform.jwt import JwtVerifier
from tests.api.test_web_mfa import (
    CREDENTIALS,
    CSRF,
    ORIGIN,
    PROBLEMS,
    Aal2AfterVerify,
    Clock,
    Memberships,
    Mfa,
)
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.log_capture import capture_logs
from tests.session_fakes import USER, Auth, Store, tokens

pytestmark = pytest.mark.anyio

OPTIONS = {"publicKey": {"challenge": "c2VjcmV0LWNoYWxsZW5nZQ", "rp": {"id": "ht-co.app"}}}


def credential(origin: str = ORIGIN) -> dict[str, Any]:
    client_data = json.dumps({"type": "webauthn.get", "origin": origin, "challenge": "x"})
    encoded = base64.urlsafe_b64encode(client_data.encode()).rstrip(b"=").decode()
    return {
        "id": "cred-1",
        "rawId": "cred-1",
        "type": "public-key",
        "response": {"clientDataJSON": encoded, "signature": "c2lnbmF0dXJlLXNlY3JldA"},
    }


@dataclass
class Provider:
    mfa: Mfa
    refuse: bool = False

    async def enroll_passkey(self, access_token: str, *, friendly_name: str) -> str:  # noqa: ARG002
        factor_id = f"pk-{len(self.mfa.factors) + 1}"
        self.mfa.factors.append(Factor(factor_id, False, friendly_name, PASSKEY))
        return factor_id

    async def challenge_passkey(self, access_token: str, factor_id: str) -> PasskeyChallenge:  # noqa: ARG002
        verified = any(f.id == factor_id and f.verified for f in self.mfa.factors)
        ceremony = Ceremony.REQUEST if verified else Ceremony.CREATE
        return PasskeyChallenge(factor_id, "ch-1", ceremony, OPTIONS)

    async def verify_passkey(
        self,
        access_token: str,  # noqa: ARG002
        factor_id: str,
        challenge_id: str,  # noqa: ARG002
        ceremony: Ceremony,  # noqa: ARG002
        credential: Mapping[str, Any],  # noqa: ARG002
    ) -> IssuedTokens:
        if self.refuse:
            raise CredentialsRefusedError
        self.mfa.factors = [
            replace(f, verified=True) if f.id == factor_id else f for f in self.mfa.factors
        ]
        return tokens(50)  # an aal2 token, as after the authenticator app


@dataclass
class Audit:
    changes: list[tuple[uuid.UUID, str, str, str]] = field(default_factory=list)

    async def factor_changed(
        self, user_id: uuid.UUID, *, factor_id: str, kind: str, change: str
    ) -> None:
        self.changes.append((user_id, factor_id, kind, change))


@dataclass
class World:
    mfa: Mfa = field(default_factory=Mfa)
    audit: Audit = field(default_factory=Audit)
    store: Store = field(default_factory=Store)
    on: bool = True

    def __post_init__(self) -> None:
        self.provider = Provider(self.mfa)


@pytest.fixture
def world() -> World:
    return World()


async def _client(world: World) -> AsyncIterator[httpx.AsyncClient]:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    web = WebAuth(
        auth=Auth(),
        tokens=Aal2AfterVerify(),
        store=world.store,
        allowed_origins=(ORIGIN,),
        mfa=world.mfa,
        passkeys=world.provider if world.on else None,
        factor_audit=world.audit,
    )
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships()), web_auth=web, clock=Clock()
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        assert (
            await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)
        ).status_code == 200
        yield http


@pytest.fixture
async def client(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async for http in _client(world):
        yield http


async def _with_app(client: httpx.AsyncClient) -> None:
    """The first factor is always the authenticator app; its code makes the session aal2."""
    await client.post("/v1/auth/mfa/enroll", json={"name": "Phone app"}, headers=CSRF)
    verified = await client.post(
        "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "123456"}, headers=CSRF
    )
    assert verified.status_code == 200


async def _add_passkey(client: httpx.AsyncClient, name: str = "MacBook") -> str:
    started = await client.post("/v1/auth/mfa/passkeys", json={"name": name}, headers=CSRF)
    assert started.status_code == 200, started.text
    factor_id = started.json()["factor_id"]
    finished = await client.post(
        f"/v1/auth/mfa/passkeys/{factor_id}/verify",
        json={"challenge_id": "ch-1", "credential": credential()},
        headers=CSRF,
    )
    assert finished.status_code == 200, finished.text
    return str(factor_id)


async def test_status__lists_both_kinds_and_says_passkeys_are_offered(
    client: httpx.AsyncClient,
) -> None:
    await _with_app(client)
    await _add_passkey(client)
    body = (await client.get("/v1/auth/mfa")).json()
    assert body["passkeys"] is True
    assert [(f["kind"], f["verified"]) for f in body["factors"]] == [
        ("totp", True),
        ("webauthn", True),
    ]


async def test_add__the_options_for_the_browser_then_audited(
    client: httpx.AsyncClient, world: World
) -> None:
    await _with_app(client)
    started = await client.post("/v1/auth/mfa/passkeys", json={"name": "MacBook"}, headers=CSRF)
    assert started.json() == {
        "factor_id": "pk-2",
        "challenge_id": "ch-1",
        "ceremony": "create",
        "options": OPTIONS,
    }
    assert started.headers["cache-control"] == "no-store"
    finished = await client.post(
        "/v1/auth/mfa/passkeys/pk-2/verify",
        json={"challenge_id": "ch-1", "credential": credential()},
        headers=CSRF,
    )
    assert finished.json() == {"assurance": "aal2"}
    assert world.audit.changes == [
        (USER, "f1", "totp", "enrolled"),
        (USER, "pk-2", "webauthn", "enrolled"),
    ]


async def test_add__needs_an_aal2_session(client: httpx.AsyncClient, world: World) -> None:
    answer = await client.post("/v1/auth/mfa/passkeys", json={"name": "MacBook"}, headers=CSRF)
    assert answer.status_code == 403
    assert answer.json()["type"] == PROBLEMS + "step-up-required"
    assert world.mfa.factors == []


async def test_sign_in__confirm_with_the_passkey_then_aal2(world: World) -> None:
    async for setup in _client(world):
        await _with_app(setup)
        await _add_passkey(setup)
    # A new browser: signed in with the password only, the passkey confirms the second step.
    async for fresh in _client(world):
        assert (await fresh.get("/v1/auth/mfa")).json()["assurance"] == "aal1"
        challenge = await fresh.post("/v1/auth/mfa/passkeys/challenge", headers=CSRF)
        assert challenge.json()["ceremony"] == "request"
        confirmed = await fresh.post(
            "/v1/auth/step-up/passkey",
            json={
                "factor_id": challenge.json()["factor_id"],
                "challenge_id": "ch-1",
                "credential": credential(),
            },
            headers=CSRF,
        )
        assert confirmed.status_code == 204
        assert (await fresh.get("/v1/auth/mfa")).json()["assurance"] == "aal2"


async def test_sign_in__the_code_still_works_as_the_fallback(world: World) -> None:
    async for setup in _client(world):
        await _with_app(setup)
        await _add_passkey(setup)
    async for fresh in _client(world):
        answer = await fresh.post("/v1/auth/step-up", json={"code": "123456"}, headers=CSRF)
        assert answer.status_code == 204
        assert (await fresh.get("/v1/auth/mfa")).json()["assurance"] == "aal2"


async def test_sign_in__a_refused_passkey_is_401(world: World) -> None:
    async for setup in _client(world):
        await _with_app(setup)
        await _add_passkey(setup)
    world.provider.refuse = True
    async for fresh in _client(world):
        answer = await fresh.post(
            "/v1/auth/step-up/passkey",
            json={"factor_id": "pk-2", "challenge_id": "ch-1", "credential": credential()},
            headers=CSRF,
        )
        assert answer.status_code == 401
        assert answer.json()["type"] == PROBLEMS + "passkey-refused"


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:5173"])
async def test_origin__a_credential_from_another_site_is_refused(
    client: httpx.AsyncClient, origin: str
) -> None:
    """AC-5: only the web app's origins (here production's); Auth checks its RP config again."""
    await _with_app(client)
    factor_id = await _add_passkey(client)
    answer = await client.post(
        "/v1/auth/step-up/passkey",
        json={"factor_id": factor_id, "challenge_id": "ch-1", "credential": credential(origin)},
        headers=CSRF,
    )
    assert answer.status_code == 401
    assert answer.json()["type"] == PROBLEMS + "passkey-refused"


async def test_remove__a_passkey_audited_but_never_the_last_app(
    client: httpx.AsyncClient, world: World
) -> None:
    await _with_app(client)
    factor_id = await _add_passkey(client)

    last_app = await client.delete("/v1/auth/mfa/factors/f1", headers=CSRF)
    removed = await client.delete(f"/v1/auth/mfa/factors/{factor_id}", headers=CSRF)
    alone = await client.delete("/v1/auth/mfa/factors/f1", headers=CSRF)
    unknown = await client.delete("/v1/auth/mfa/factors/nope", headers=CSRF)

    assert last_app.status_code == 409
    assert last_app.json()["type"] == PROBLEMS + "last-factor"
    assert removed.status_code == 204
    assert alone.status_code == 409
    assert unknown.status_code == 404
    assert [f.id for f in world.mfa.factors] == ["f1"]
    assert world.audit.changes[-1] == (USER, factor_id, "webauthn", "removed")


async def test_remove__needs_an_aal2_session(world: World) -> None:
    async for setup in _client(world):
        await _with_app(setup)
        await _add_passkey(setup)
    async for fresh in _client(world):
        answer = await fresh.delete("/v1/auth/mfa/factors/pk-2", headers=CSRF)
        assert answer.status_code == 403
        assert answer.json()["type"] == PROBLEMS + "step-up-required"
    assert len(world.mfa.factors) == 2


async def test_unsafe_routes__need_the_web_apps_csrf_header(client: httpx.AsyncClient) -> None:
    await _with_app(client)
    answer = await client.post("/v1/auth/mfa/passkeys", json={"name": "x"})
    assert answer.status_code == 403
    assert answer.json()["type"] == PROBLEMS + "csrf-failed"


async def test_switched_off__the_routes_say_so_and_the_status_too(world: World) -> None:
    world.on = False
    async for client in _client(world):
        await _with_app(client)
        status = (await client.get("/v1/auth/mfa")).json()
        started = await client.post("/v1/auth/mfa/passkeys", json={"name": "x"}, headers=CSRF)
        challenge = await client.post("/v1/auth/mfa/passkeys/challenge", headers=CSRF)
        assert status["passkeys"] is False
        assert started.status_code == 404
        assert started.json()["type"] == PROBLEMS + "passkeys-unavailable"
        assert challenge.status_code == 404


async def test_a_huge_credential_is_refused(client: httpx.AsyncClient) -> None:
    await _with_app(client)
    big = credential() | {"padding": "x" * 20_000}
    answer = await client.post(
        "/v1/auth/mfa/passkeys/pk-2/verify",
        json={"challenge_id": "ch-1", "credential": big},
        headers=CSRF,
    )
    assert answer.status_code == 422


async def test_no_credential_material_in_the_logs(client: httpx.AsyncClient) -> None:
    """AC-4: neither the options' challenge nor the credential's signature is ever logged."""
    with capture_logs("DEBUG") as logs:
        await _with_app(client)
        await _add_passkey(client)
        await client.post("/v1/auth/mfa/passkeys/challenge", headers=CSRF)
    text = logs.raw()
    assert "/v1/auth/mfa/passkeys" in text  # the requests were logged
    assert "c2VjcmV0LWNoYWxsZW5nZQ" not in text
    assert "c2lnbmF0dXJlLXNlY3JldA" not in text
    assert "clientDataJSON" not in text
