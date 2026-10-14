"""JWT verification against the auth provider's JWKS (slice 2.3, AC-2.3.1 and AC-2.3.4)."""

import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, override

import httpx
import jwt
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_platform.http import new_async_http_client
from htco_platform.jwt import (
    HttpJwksSource,
    InvalidTokenError,
    JwtVerifier,
    KeySetUnavailableError,
    TokenProblem,
)
from tests.auth_tokens import AUDIENCE, ISSUER, USER_ID, Clock, FakeJwks, SigningKey, claims

pytestmark = pytest.mark.anyio


def verifier(jwks: FakeJwks, clock: Clock | None = None) -> JwtVerifier:
    return JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks, monotonic=clock or Clock())


async def test_verify__valid_token__returns_the_user_and_session_strength() -> None:
    key = SigningKey("k1")
    token = await verifier(FakeJwks(key)).verify(key.sign(claims(aal="aal2")))

    assert token.subject == USER_ID
    assert token.assurance == "aal2"
    assert token.session_id == "session-1"
    assert token.expires_at.tzinfo is UTC
    assert token.expires_at > datetime.now(UTC)
    assert token.claims["sub"] == str(USER_ID)


async def test_verify__token_without_aal__is_treated_as_aal1() -> None:
    key = SigningKey("k1")
    token = await verifier(FakeJwks(key)).verify(key.sign(claims(aal=None)))
    assert token.assurance == "aal1"


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"exp": int(time.time()) - 10}, TokenProblem.EXPIRED),
        ({"aud": "someone-else"}, TokenProblem.WRONG_AUDIENCE),
        ({"iss": "https://evil.example/auth/v1"}, TokenProblem.WRONG_ISSUER),
        ({"exp": None}, TokenProblem.MISSING_CLAIM),
        ({"sub": None}, TokenProblem.MISSING_CLAIM),
        ({"role": None}, TokenProblem.MISSING_CLAIM),
        ({"sub": "not-a-uuid"}, TokenProblem.MALFORMED),
        ({"aal": "aal9"}, TokenProblem.MALFORMED),
        ({"role": "anon"}, TokenProblem.NOT_A_USER),
        ({"role": "service_role"}, TokenProblem.NOT_A_USER),
        ({"nbf": int(time.time()) + 600}, TokenProblem.MALFORMED),
    ],
    ids=[
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "no-exp",
        "no-sub",
        "no-role",
        "sub-not-uuid",
        "unknown-aal",
        "anon-role",
        "service-role",
        "not-yet-valid",
    ],
)
async def test_verify__bad_claims__names_the_problem(
    overrides: dict[str, object], problem: TokenProblem
) -> None:
    key = SigningKey("k1")
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(key)).verify(key.sign(claims(**overrides)))
    assert caught.value.problem is problem


async def test_verify__signed_by_another_key_with_a_known_kid__is_a_bad_signature() -> None:
    trusted = SigningKey("k1")
    forged = SigningKey("k1")
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(trusted)).verify(forged.sign(claims()))
    assert caught.value.problem is TokenProblem.BAD_SIGNATURE


async def test_verify__tampered_payload__is_a_bad_signature() -> None:
    key = SigningKey("k1")
    header, _, signature = key.sign(claims()).split(".")
    other_payload = key.sign(claims(sub=str(uuid.uuid4()))).split(".")[1]
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(key)).verify(f"{header}.{other_payload}.{signature}")
    assert caught.value.problem is TokenProblem.BAD_SIGNATURE


async def test_verify__alg_none__is_rejected() -> None:
    unsigned = jwt.encode(claims(), "", algorithm="none", headers={"kid": "k1"})
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(SigningKey("k1"))).verify(unsigned)
    assert caught.value.problem is TokenProblem.BAD_SIGNATURE


async def test_verify__hs256_signed_with_the_public_key__is_rejected() -> None:
    key = SigningKey("k1")
    secret = b"a-shared-secret-of-at-least-thirty-two-bytes!"
    forged = jwt.encode(claims(), secret, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(key)).verify(forged)
    assert caught.value.problem is TokenProblem.BAD_SIGNATURE


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "a.b"])
async def test_verify__not_a_jwt__is_malformed(token: str) -> None:
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(SigningKey("k1"))).verify(token)
    assert caught.value.problem is TokenProblem.MALFORMED


async def test_verify__token_without_kid__is_an_unknown_key() -> None:
    key = SigningKey("k1")
    token = jwt.encode(claims(), key.private, algorithm="ES256")
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(FakeJwks(key)).verify(token)
    assert caught.value.problem is TokenProblem.UNKNOWN_KEY


# AC-2.3.4: the key set is cached for a bounded time and refreshed on an unknown key ID ----------


async def test_jwks__is_fetched_once_while_fresh() -> None:
    key = SigningKey("k1")
    jwks, clock = FakeJwks(key), Clock()
    subject = verifier(jwks, clock)

    for _ in range(3):
        await subject.verify(key.sign(claims()))
        clock.now += 60

    assert jwks.fetches == 1


async def test_jwks__is_refetched_after_its_lifetime() -> None:
    key = SigningKey("k1")
    jwks, clock = FakeJwks(key), Clock()
    subject = verifier(jwks, clock)

    await subject.verify(key.sign(claims()))
    clock.now += JwtVerifier.DEFAULT_MAX_AGE_SECONDS + 1
    await subject.verify(key.sign(claims()))

    assert jwks.fetches == 2


async def test_jwks__unknown_kid__refreshes_and_accepts_a_rotated_key() -> None:
    old, new = SigningKey("old"), SigningKey("new")
    jwks, clock = FakeJwks(old), Clock()
    subject = verifier(jwks, clock)
    await subject.verify(old.sign(claims()))

    jwks.keys.append(new)  # the provider rotates its signing key
    clock.now += JwtVerifier.DEFAULT_REFRESH_COOLDOWN_SECONDS + 1
    token = await subject.verify(new.sign(claims()))

    assert token.subject == USER_ID
    assert jwks.fetches == 2


async def test_jwks__unknown_kids_within_the_cooldown__do_not_refetch() -> None:
    key = SigningKey("k1")
    jwks, clock = FakeJwks(key), Clock()
    subject = verifier(jwks, clock)
    await subject.verify(key.sign(claims()))

    for attempt in range(5):
        with pytest.raises(InvalidTokenError) as caught:
            await subject.verify(key.sign(claims(), kid=f"unknown-{attempt}"))
        assert caught.value.problem is TokenProblem.UNKNOWN_KEY

    assert jwks.fetches == 1


async def test_jwks__unreachable_and_nothing_cached__fails_closed() -> None:
    jwks = FakeJwks(SigningKey("k1"))
    jwks.fail = True
    with pytest.raises(KeySetUnavailableError):
        await verifier(jwks).verify(SigningKey("k1").sign(claims()))


async def test_jwks__unreachable_after_expiry__does_not_use_stale_keys() -> None:
    key = SigningKey("k1")
    jwks, clock = FakeJwks(key), Clock()
    subject = verifier(jwks, clock)
    await subject.verify(key.sign(claims()))

    jwks.fail = True
    clock.now += JwtVerifier.DEFAULT_MAX_AGE_SECONDS + 1
    with pytest.raises(KeySetUnavailableError):
        await subject.verify(key.sign(claims()))


async def test_jwks__failed_refresh_for_an_unknown_kid__keeps_the_current_keys() -> None:
    key = SigningKey("k1")
    jwks, clock = FakeJwks(key), Clock()
    subject = verifier(jwks, clock)
    await subject.verify(key.sign(claims()))

    jwks.fail = True
    clock.now += JwtVerifier.DEFAULT_REFRESH_COOLDOWN_SECONDS + 1
    with pytest.raises(InvalidTokenError) as caught:
        await subject.verify(key.sign(claims(), kid="forged"))
    assert caught.value.problem is TokenProblem.UNKNOWN_KEY
    assert (await subject.verify(key.sign(claims()))).subject == USER_ID


async def test_jwks__keys_that_cannot_sign_es256__are_ignored() -> None:
    key = SigningKey("k1")

    class MixedJwks(FakeJwks):
        @override
        async def fetch(self) -> Mapping[str, Any]:
            document = await super().fetch()
            junk = [
                {"kid": "rsa", "kty": "RSA", "alg": "RS256", "n": "AQAB", "e": "AQAB"},
                {"kid": "broken", "kty": "EC"},
                {"kid": "hmac", "kty": "oct", "k": "c2VjcmV0"},
            ]
            return {"keys": [*junk, *document["keys"]]}

    jwks = MixedJwks(key)
    token = await verifier(jwks).verify(key.sign(claims()))
    assert token.subject == USER_ID
    with pytest.raises(InvalidTokenError) as caught:
        await verifier(jwks).verify(key.sign(claims(), kid="hmac"))
    assert caught.value.problem is TokenProblem.UNKNOWN_KEY


@pytest.mark.parametrize("document", [{}, {"keys": "nope"}, [], {"keys": [1, "x"]}])
async def test_jwks__malformed_document__is_unavailable(document: object) -> None:
    class Odd(FakeJwks):
        @override
        async def fetch(self) -> Any:
            return document

    with pytest.raises(KeySetUnavailableError):
        await verifier(Odd()).verify(SigningKey("k1").sign(claims()))


# The HTTP key source ---------------------------------------------------------------------------


async def test_http_jwks_source__fetches_the_document() -> None:
    key = SigningKey("k1")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"keys": [key.jwk()]})

    async with new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(handler)
    ) as client:
        source = HttpJwksSource(client, f"{ISSUER}/.well-known/jwks.json")
        document = await source.fetch()

    assert document == {"keys": [key.jwk()]}
    assert str(seen[0].url) == f"{ISSUER}/.well-known/jwks.json"


async def test_http_jwks_source__error_status__raises() -> None:
    async with new_async_http_client(
        tracer_provider=TracerProvider(),
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await HttpJwksSource(client, "https://x/jwks.json").fetch()


async def test_jwt_verifier__repr__names_the_issuer() -> None:
    assert ISSUER in repr(verifier(FakeJwks()))
