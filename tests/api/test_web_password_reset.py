"""Password reset in the web app (slice 3.5 part 3, AC-3.5.6).

`POST /v1/auth/password/forgot` always answers 202 the same way, so it never says whether an
account exists; Supabase sends the email. The link lands on the API's link callback with a
one-time token, which the API redeems on the server and keeps sealed in an `HttpOnly` cookie
(Codex security review, owner gate v4); the page posts only the new password to
`POST /v1/auth/password/reset`, which answers exactly like a sign-in and signs out every other
session of the user.
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
from htco_application.password_reset import MAX_RESET_REQUESTS_PER_EMAIL
from htco_application.web_sessions import MAX_FAILURES_PER_IP
from htco_domain.identity import Role
from htco_platform.crypto import Sealer
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.log_capture import LogCapture
from tests.session_fakes import (
    NEW_PASSWORD,
    NOW,
    RESET_ACCESS,
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
LINK = "/v1/auth/links/callback"
RESET_PAGE = "https://dash.ht-co.app/reset-password"
SEALER = Sealer(b"k" * 32)
BODY = {"password": NEW_PASSWORD}


class Memberships:
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return [Membership(ORG, Role.ADMIN, frozenset())] if user.user_id == USER else []


@pytest.fixture
def auth() -> Auth:
    return Auth()


@pytest.fixture
def store() -> Store:
    return Store()


def _web(auth: Auth, store: Store) -> WebAuth:
    return WebAuth(
        auth=auth,
        mfa=FakeAuthenticator(auth),
        tokens=Tokens(),
        store=store,
        allowed_origins=(ORIGIN,),
        password_reset_redirect=RESET_REDIRECT,
        link_pages={"recovery": RESET_PAGE, "invite": "https://dash.ht-co.app/accept-invite"},
        link_sealer=SEALER,
    )


async def _open_link(
    http: httpx.AsyncClient,
    token_hash: str = "recovery-hash",  # noqa: S107 - a fake link token
) -> httpx.Response:
    """Opens the emailed link, as the browser does: the callback sets the link cookie."""
    return await http.get(LINK, params={"token_hash": token_hash, "type": "recovery"})


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
    async with _client(_web(auth, store)) as http:
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


async def test_forgot__not_json__422(client: httpx.AsyncClient) -> None:
    response = await client.post(
        FORGOT, content=b"{", headers=CSRF | {"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"


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


# Opening the link -------------------------------------------------------------------------------


async def test_link__redeemed_on_the_server__clean_redirect_and_a_sealed_httponly_cookie(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    response = await _open_link(client)

    assert response.status_code == 303
    assert response.headers["location"] == RESET_PAGE
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{LINK_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/", "Max-Age=900"):
        assert attribute.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    # The tokens are sealed: neither the redirect nor the cookie shows them.
    assert RESET_ACCESS not in cookie
    assert "reset-refresh" not in cookie
    assert auth.redeemed == {"recovery-hash"}


@pytest.mark.parametrize("token_hash", ["forged-hash", "invite-hash"], ids=["forged", "other"])
async def test_link__refused__to_the_page_with_link_invalid_and_no_cookie(
    client: httpx.AsyncClient, token_hash: str
) -> None:
    response = await _open_link(client, token_hash)

    assert response.status_code == 303
    assert response.headers["location"] == f"{RESET_PAGE}?link=invalid"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert (await client.post(RESET, json=BODY, headers=CSRF)).status_code == 401


async def test_link__used_twice__the_second_opening_is_invalid(client: httpx.AsyncClient) -> None:
    await _open_link(client)
    second = await _open_link(client)

    assert second.headers["location"] == f"{RESET_PAGE}?link=invalid"


async def test_link__repeated_failures__to_the_page_with_link_too_many(
    client: httpx.AsyncClient,
) -> None:
    for n in range(MAX_FAILURES_PER_IP):
        await _open_link(client, f"forged-{n}")

    response = await _open_link(client)

    assert response.status_code == 303
    assert response.headers["location"] == f"{RESET_PAGE}?link=too-many"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "params",
    [
        {"type": "recovery"},
        {"token_hash": "x", "type": "signup"},
        {"token_hash": "", "type": "invite"},
    ],
    ids=["no-token", "unknown-type", "empty-token"],
)
async def test_link__malformed__422(client: httpx.AsyncClient, params: dict[str, str]) -> None:
    response = await client.get(LINK, params=params)

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"


async def test_link__no_page_or_sealer__503(auth: Auth, store: Store) -> None:
    web = WebAuth(auth=auth, tokens=Tokens(), store=store, allowed_origins=(ORIGIN,))
    async with _client(web) as http:
        response = await _open_link(http)

    assert response.status_code == 503
    assert auth.redeemed == set()


# Setting the new password ----------------------------------------------------------------------


async def test_reset__sets_the_password_and_signs_in_like_sign_in(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    await _open_link(client)

    response = await client.post(RESET, json=BODY, headers=CSRF)

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER), "assurance": "aal1"}
    assert auth.passwords_set == [(RESET_ACCESS, NEW_PASSWORD)]
    assert "access" not in response.text
    assert "refresh" not in response.text
    cookies = response.headers.get_list("set-cookie")
    [session] = [c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")]
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in session.lower()
    [link] = [c for c in cookies if c.startswith(f"{LINK_COOKIE}=")]
    assert "max-age=0" in link.lower()  # the link cookie is spent
    assert response.headers["cache-control"] == "no-store"
    me = await client.get("/v1/me")
    assert me.status_code == 200


async def test_reset__signs_out_the_users_other_sessions(auth: Auth, store: Store) -> None:
    web = _web(auth, store)
    async with _client(web) as old_browser, _client(web) as new_browser:
        signed = await old_browser.post(
            "/v1/auth/sign-in",
            json={"email": "owner@example.test", "password": auth.password},
            headers=CSRF,
        )
        assert signed.status_code == 200
        await _open_link(new_browser)

        reset = await new_browser.post(RESET, json=BODY, headers=CSRF)

        assert reset.status_code == 200
        assert (await old_browser.get("/v1/auth/session")).status_code == 401
        assert (await new_browser.get("/v1/auth/session")).status_code == 200


async def test_reset__no_link_cookie__401_reset_link_refused_without_a_session(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    response = await client.post(RESET, json=BODY, headers=CSRF)

    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "reset-link-refused"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert auth.passwords_set == []


@pytest.mark.parametrize(
    "cookie", ["not-sealed", "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"], ids=["junk", "forged"]
)
async def test_reset__tampered_link_cookie__401(
    client: httpx.AsyncClient, auth: Auth, cookie: str
) -> None:
    client.cookies.set(LINK_COOKIE, cookie, domain="api.test")

    response = await client.post(RESET, json=BODY, headers=CSRF)

    assert response.status_code == 401
    assert auth.passwords_set == []


async def test_reset__an_invitations_link_cookie__401(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    await client.get(LINK, params={"token_hash": "invite-hash", "type": "invite"})

    response = await client.post(RESET, json=BODY, headers=CSRF)

    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "reset-link-refused"
    assert auth.passwords_set == []


async def test_reset__link_cookie_older_than_its_lifetime__401(auth: Auth, store: Store) -> None:
    from datetime import timedelta  # noqa: PLC0415 - local to this test

    now = [NOW]
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships()),
            web_auth=_web(auth, store),
            clock=lambda: now[0],
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        await _open_link(http)
        now[0] = NOW + timedelta(minutes=15)

        response = await http.post(RESET, json=BODY, headers=CSRF)

    assert response.status_code == 401
    assert auth.passwords_set == []


async def test_reset__weak_password__422_weak_password_and_the_link_still_works(
    client: httpx.AsyncClient,
) -> None:
    await _open_link(client)

    weak = await client.post(RESET, json={"password": "weak but long enough"}, headers=CSRF)
    retried = await client.post(RESET, json=BODY, headers=CSRF)

    assert weak.status_code == 422
    assert weak.json()["type"] == PROBLEMS + "weak-password"
    assert retried.status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"password": "eleven char"},
        {"password": "x" * 129},
        {"code": "12345"},
        {"code": "abcdef"},
    ],
    ids=["shorter-than-12", "longer-than-128", "short-code", "letters-code"],
)
async def test_reset__malformed__422_validation(
    client: httpx.AsyncClient, auth: Auth, change: dict[str, str]
) -> None:
    await _open_link(client)

    response = await client.post(RESET, json=BODY | change, headers=CSRF)

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"
    assert auth.passwords_set == []


async def test_reset__fails_csrf_checks__403(client: httpx.AsyncClient, auth: Auth) -> None:
    await _open_link(client)

    response = await client.post(
        RESET, json=BODY, headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    )

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert auth.passwords_set == []


async def test_reset__authenticator_user__403_code_required_then_200_with_the_code(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    auth.mfa_enrolled = True
    await _open_link(client)

    asked = await client.post(RESET, json=BODY, headers=CSRF)
    wrong = await client.post(RESET, json=BODY | {"code": "000000"}, headers=CSRF)
    right = await client.post(RESET, json=BODY | {"code": RIGHT_CODE}, headers=CSRF)

    assert asked.status_code == 403
    assert asked.json()["type"] == PROBLEMS + "mfa-code-required"
    assert wrong.status_code == 401
    assert wrong.json()["type"] == PROBLEMS + "mfa-code-refused"
    assert right.status_code == 200
    assert f"{SESSION_COOKIE}=" in right.headers["set-cookie"]
    assert auth.passwords_set == [("aal2-access", NEW_PASSWORD)]


async def test_reset__not_configured__503() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="https://api.test"
    ) as http:
        response = await http.post(RESET, json=BODY, headers=CSRF)
    assert response.status_code == 503


async def test_reset__the_current_password__422_same_password(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    await _open_link(client)

    response = await client.post(RESET, json={"password": auth.password}, headers=CSRF)

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "same-password"


async def test_reset__replayed_link_cookie__401_reset_link_refused(
    client: httpx.AsyncClient, auth: Auth
) -> None:
    await _open_link(client)
    captured = client.cookies[LINK_COOKIE]
    first = await client.post(RESET, json=BODY, headers=CSRF)
    client.cookies.set(LINK_COOKIE, captured, domain="api.test")

    replay = await client.post(RESET, json={"password": "another long passphrase"}, headers=CSRF)

    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.json()["type"] == PROBLEMS + "reset-link-refused"
    assert len(auth.passwords_set) == 1
