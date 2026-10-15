"""Password reset in the web app (slice 3.5 part 3, AC-3.5.6).

`POST /v1/auth/password/forgot` always answers 202 the same way, so it never says whether an
account exists; Supabase sends the email. The link lands on the web app with a recovery session's
tokens in the URL fragment; the page posts them once with the new password to
`POST /v1/auth/password/reset`, which answers exactly like a sign-in and signs out every other
session of the user.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import SESSION_COOKIE, WebAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_application.password_reset import MAX_RESET_REQUESTS_PER_EMAIL
from htco_application.web_sessions import MAX_FAILURES_PER_IP
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.log_capture import LogCapture
from tests.session_fakes import (
    NEW_PASSWORD,
    NOW,
    RESET_REDIRECT,
    RIGHT_CODE,
    USER,
    Auth,
    Store,
    Tokens,
)
from tests.session_fakes import (
    Authenticator as FakeAuthenticator,
)

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
PROBLEMS = "https://ht-co.app/problems/"
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
CSRF = {"X-HTCO-CSRF": "1", "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
FORGOT = "/v1/auth/password/forgot"
RESET = "/v1/auth/password/reset"


class Memberships:
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return [Membership(ORG, Role.ADMIN, frozenset())] if user.user_id == USER else []


@pytest.fixture
def auth() -> Auth:
    return Auth()


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def body(auth: Auth) -> dict[str, str]:
    access, refresh = auth.recovery()
    return {"access_token": access, "refresh_token": refresh, "password": NEW_PASSWORD}


def _client(web: WebAuth) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships()), web_auth=web, clock=lambda: NOW
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    return httpx.AsyncClient(transport=transport, base_url="https://api.test")


@pytest.fixture
async def client(auth: Auth, store: Store) -> AsyncIterator[httpx.AsyncClient]:
    web = WebAuth(
        auth=auth,
        mfa=FakeAuthenticator(auth),
        tokens=Tokens(),
        store=store,
        allowed_origins=(ORIGIN,),
        password_reset_redirect=RESET_REDIRECT,
    )
    async with _client(web) as http:
        yield http


# Asking for a link -----------------------------------------------------------------------------


async def test_forgot__asks_supabase_to_email_the_link_and_answers_202_with_no_body(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    response = await client.post(FORGOT, json={"email": " Owner@Example.test "}, headers=CSRF)

    assert response.status_code == 202
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert auth.reset_emails == [("owner@example.test", RESET_REDIRECT)]


async def test_forgot__unknown_email__the_same_answer(client: httpx.AsyncClient) -> None:
    known = await client.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)
    unknown = await client.post(FORGOT, json={"email": "nobody@example.test"}, headers=CSRF)

    assert (unknown.status_code, unknown.content) == (known.status_code, known.content)
    assert set(unknown.headers) == set(known.headers)


async def test_forgot__supabase_fails__still_202_and_logged_without_the_email(
    client: httpx.AsyncClient, auth: Auth, logs: LogCapture
) -> None:
    auth.reset_email_fails = True

    response = await client.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)

    assert response.status_code == 202
    assert response.content == b""
    assert any(line["event"] == "auth.password_reset_email_failed" for line in logs.lines())
    assert "owner@example.test" not in logs.raw()


async def test_forgot__sent__logged_without_the_email(
    client: httpx.AsyncClient, logs: LogCapture
) -> None:
    await client.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)

    assert any(line["event"] == "auth.password_reset_email_sent" for line in logs.lines())
    assert "owner@example.test" not in logs.raw()


async def test_forgot__too_many_for_one_email__429_with_retry_after(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    for _ in range(MAX_RESET_REQUESTS_PER_EMAIL):
        assert (
            await client.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)
        ).status_code == 202

    response = await client.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)

    assert response.status_code == 429
    assert response.json()["type"] == PROBLEMS + "too-many-attempts"
    assert int(response.headers["retry-after"]) > 0
    assert len(auth.reset_emails) == MAX_RESET_REQUESTS_PER_EMAIL


@pytest.mark.parametrize(
    "payload",
    [{}, {"email": ""}, {"email": "x" * 321}, {"email": 5}],
    ids=["missing", "empty", "too-long", "not-a-string"],
)
async def test_forgot__malformed__422(
    client: httpx.AsyncClient, auth: Auth, payload: dict[str, object]
) -> None:
    response = await client.post(FORGOT, json=payload, headers=CSRF)

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"
    assert auth.reset_emails == []


async def test_forgot__not_json__400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        FORGOT, content=b"{", headers=CSRF | {"Content-Type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        {"X-HTCO-CSRF": "1", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    ],
    ids=["no-header", "cross-site-origin"],
)
async def test_forgot__fails_csrf_checks__403(
    client: httpx.AsyncClient, auth: Auth, headers: dict[str, str]
) -> None:
    response = await client.post(FORGOT, json={"email": "owner@example.test"}, headers=headers)

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert auth.reset_emails == []


async def test_forgot__redirect_not_configured__503(auth: Auth, store: Store) -> None:
    web = WebAuth(auth=auth, tokens=Tokens(), store=store, allowed_origins=(ORIGIN,))
    async with _client(web) as http:
        response = await http.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)

    assert response.status_code == 503
    assert response.json()["type"] == PROBLEMS + "auth-unavailable"
    assert auth.reset_emails == []


async def test_forgot__web_sign_in_not_configured__503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="https://api.test"
    ) as http:
        response = await http.post(FORGOT, json={"email": "owner@example.test"}, headers=CSRF)
    assert response.status_code == 503


# Setting the new password ----------------------------------------------------------------------


async def test_reset__sets_the_password_and_signs_in_like_sign_in(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str]
) -> None:
    response = await client.post(RESET, json=body, headers=CSRF)

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER), "assurance": "aal1"}
    assert auth.passwords_set == [(body["access_token"], NEW_PASSWORD)]
    assert "access" not in response.text
    assert "refresh" not in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert response.headers["cache-control"] == "no-store"
    me = await client.get("/v1/me")
    assert me.status_code == 200


async def test_reset__signs_out_the_users_other_sessions(
    auth: Auth, store: Store, body: dict[str, str]
) -> None:
    web = WebAuth(
        auth=auth,
        mfa=FakeAuthenticator(auth),
        tokens=Tokens(),
        store=store,
        allowed_origins=(ORIGIN,),
        password_reset_redirect=RESET_REDIRECT,
    )
    async with _client(web) as old_browser, _client(web) as new_browser:
        signed = await old_browser.post(
            "/v1/auth/sign-in",
            json={"email": "owner@example.test", "password": auth.password},
            headers=CSRF,
        )
        assert signed.status_code == 200

        reset = await new_browser.post(RESET, json=body, headers=CSRF)

        assert reset.status_code == 200
        assert (await old_browser.get("/v1/auth/session")).status_code == 401
        assert (await new_browser.get("/v1/auth/session")).status_code == 200


async def test_reset__invalid_link__401_reset_link_refused_without_a_cookie(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    response = await client.post(RESET, json=body | {"access_token": "invalid-token"}, headers=CSRF)

    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "reset-link-refused"
    assert "set-cookie" not in response.headers


async def test_reset__weak_password__422_weak_password(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    response = await client.post(
        RESET, json=body | {"password": "weak but long enough"}, headers=CSRF
    )

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "weak-password"


@pytest.mark.parametrize(
    "change",
    [
        {"password": "eleven char"},
        {"password": "x" * 129},
        {"code": "12345"},
        {"code": "abcdef"},
        {"access_token": ""},
    ],
    ids=["shorter-than-12", "longer-than-128", "short-code", "letters-code", "no-token"],
)
async def test_reset__malformed__422_validation(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str], change: dict[str, str]
) -> None:
    response = await client.post(RESET, json=body | change, headers=CSRF)

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"
    assert auth.passwords_set == []


async def test_reset__fails_csrf_checks__403(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str]
) -> None:
    response = await client.post(
        RESET, json=body, headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    )

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert auth.passwords_set == []


async def test_reset__repeated_failures__429_with_retry_after(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    for n in range(MAX_FAILURES_PER_IP):
        await client.post(RESET, json=body | {"access_token": f"invalid-{n}"}, headers=CSRF)

    response = await client.post(RESET, json=body, headers=CSRF)

    assert response.status_code == 429
    assert response.json()["type"] == PROBLEMS + "too-many-attempts"
    assert int(response.headers["retry-after"]) > 0


async def test_reset__authenticator_user__403_code_required_then_200_with_the_code(
    client: httpx.AsyncClient, auth: Auth, body: dict[str, str]
) -> None:
    auth.mfa_enrolled = True

    asked = await client.post(RESET, json=body, headers=CSRF)
    wrong = await client.post(RESET, json=body | {"code": "000000"}, headers=CSRF)
    right = await client.post(RESET, json=body | {"code": RIGHT_CODE}, headers=CSRF)

    assert asked.status_code == 403
    assert asked.json()["type"] == PROBLEMS + "mfa-code-required"
    assert wrong.status_code == 401
    assert wrong.json()["type"] == PROBLEMS + "mfa-code-refused"
    assert right.status_code == 200
    assert right.headers["set-cookie"].startswith(f"{SESSION_COOKIE}=")
    assert auth.passwords_set == [("aal2-access", NEW_PASSWORD)]


async def test_reset__not_configured__503(body: dict[str, str]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="https://api.test"
    ) as http:
        response = await http.post(RESET, json=body, headers=CSRF)
    assert response.status_code == 503
