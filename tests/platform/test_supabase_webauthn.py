"""Passkeys through Supabase Auth's WebAuthn MFA factor (issue #113), against a mock transport:
enroll, challenge (its options kept out of `repr`), verify with the credential passed through,
refusals, and Auth's WebAuthn switched off."""

import json

import httpx
import pytest

from htco_platform.supabase_auth import (
    AuthServiceError,
    InvalidCredentialsError,
    WebAuthnChallenge,
    WebAuthnDisabledError,
)
from tests.platform.test_supabase_auth import TOKENS, auth

pytestmark = pytest.mark.anyio

OPTIONS = {"publicKey": {"challenge": "c2VjcmV0", "rp": {"id": "localhost", "name": "HTCO"}}}
CREDENTIAL = {"id": "cred", "rawId": "cred", "type": "public-key", "response": {"x": "y"}}


async def test_enroll__creates_a_webauthn_factor_with_its_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/auth/v1/factors")
        assert request.headers["authorization"] == "Bearer access-1"
        assert json.loads(request.content) == {"factor_type": "webauthn", "friendly_name": "Mac"}
        return httpx.Response(200, json={"id": "pk-1", "type": "webauthn", "friendly_name": "Mac"})

    subject, client = auth(handler)
    async with client:
        assert await subject.enroll_webauthn("access-1", friendly_name="Mac") == "pk-1"


@pytest.mark.parametrize("ceremony", ["create", "request"])
async def test_challenge__returns_the_ceremony_and_the_options(ceremony: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/factors/pk-1/challenge"
        return httpx.Response(
            200,
            json={
                "id": "ch-1",
                "type": "webauthn",
                "expires_at": 1_800_000_000,
                "webauthn": {"type": ceremony, "credential_options": OPTIONS},
            },
        )

    subject, client = auth(handler)
    async with client:
        challenge = await subject.challenge_webauthn("access-1", "pk-1")

    assert challenge == WebAuthnChallenge("ch-1", ceremony, OPTIONS)
    assert "c2VjcmV0" not in repr(challenge)


@pytest.mark.parametrize(
    "body",
    [{"id": "ch-1"}, {"id": "ch-1", "webauthn": {"type": "other", "credential_options": {}}}],
)
async def test_challenge__without_webauthn_options__is_a_service_error(
    body: dict[str, object],
) -> None:
    subject, client = auth(lambda _: httpx.Response(200, json=body))
    async with client:
        with pytest.raises(AuthServiceError):
            await subject.challenge_webauthn("access-1", "pk-1")


async def test_verify__passes_the_credential_through_and_returns_aal2_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/factors/pk-1/verify"
        assert json.loads(request.content) == {
            "challenge_id": "ch-1",
            "webauthn": {"type": "request", "credential_response": CREDENTIAL},
        }
        return httpx.Response(200, json=TOKENS | {"access_token": "access-aal2"})

    subject, client = auth(handler)
    async with client:
        tokens = await subject.verify_webauthn("access-1", "pk-1", "ch-1", "request", CREDENTIAL)
    assert tokens.access_token == "access-aal2"  # noqa: S105 - test value


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error_code": "validation_failed"}),
        httpx.Response(422, json={"error_code": "mfa_challenge_expired"}),
        httpx.Response(422, json={"error_code": "mfa_ip_address_mismatch"}),
        # Auth v2.197.0 answers 500 for an assertion that fails validation.
        httpx.Response(500, json={"msg": "Failed to validate WebAuthn MFA response"}),
    ],
)
async def test_verify__a_refused_passkey(response: httpx.Response) -> None:
    subject, client = auth(lambda _: response)
    async with client:
        with pytest.raises(InvalidCredentialsError):
            await subject.verify_webauthn("access-1", "pk-1", "ch-1", "request", CREDENTIAL)


async def test_challenge__a_server_error_is_not_a_refusal() -> None:
    subject, client = auth(lambda _: httpx.Response(500, json={}))
    async with client:
        with pytest.raises(AuthServiceError) as raised:
            await subject.challenge_webauthn("access-1", "pk-1")
    assert not isinstance(raised.value, InvalidCredentialsError)


@pytest.mark.parametrize(
    "code", ["mfa_webauthn_enroll_not_enabled", "mfa_webauthn_verify_not_enabled"]
)
async def test_webauthn_switched_off_in_auth(code: str) -> None:
    subject, client = auth(lambda _: httpx.Response(422, json={"error_code": code}))
    async with client:
        with pytest.raises(WebAuthnDisabledError):
            await subject.enroll_webauthn("access-1", friendly_name="Mac")
