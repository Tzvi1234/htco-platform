"""Etsy's OAuth 2.0 endpoints (slices 4.3 and 4.4) and the shop lookup after connecting.

Facts from developers.etsy.com/documentation/essentials/authentication (read 2026-10-15): the
authorization URL is https://www.etsy.com/oauth/connect with PKCE S256; the token endpoint takes a
form POST (`authorization_code` with `client_id`, `redirect_uri`, `code`, `code_verifier`;
`refresh_token` with `client_id`, `refresh_token`) and answers `access_token` (prefixed with the
numeric user ID and a dot), `token_type`, `expires_in` (3600), `refresh_token` and, sometimes,
`scope`. Refused grants answer 400 with `{"error": "invalid_grant"}`. No test talks to Etsy.
"""

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_application.etsy_connect import (
    EtsyAuthorizationServer,
    EtsyShopDirectory,
    OAuthInvalidGrantError,
    OAuthUnavailableError,
)
from htco_application.marketplace import MarketplaceNotFoundError
from htco_integrations.etsy import EtsyAdapter, EtsyCredentials
from htco_integrations.etsy_oauth import (
    ETSY_AUTHORIZATION_URL,
    ETSY_TOKEN_URL,
    EtsyOAuthClient,
)
from htco_platform.http import new_async_http_client
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

KEYSTRING = "fakekeystring"
SHARED_SECRET = "fake-shared-secret"  # noqa: S105 - obviously fake test value
REDIRECT = "https://dash.ht-co.app/integrations/etsy/callback"
ACCESS = "12345678.fake-access-token-for-oauth-tests"
REFRESH = "12345678.fake-refresh-token-for-oauth-tests"
CODE = "fake-authorization-code"
VERIFIER = "fake-code-verifier-" + "v" * 40

Handler = Callable[[httpx.Request], httpx.Response]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "etsy"


def token_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": ACCESS,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": REFRESH,
    }
    return body | overrides


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def form(self, index: int = -1) -> dict[str, str]:
        parsed = parse_qs(self.requests[index].content.decode())
        return {key: value for key, [value] in parsed.items()}


def oauth(recorder: Recorder) -> EtsyOAuthClient:
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(recorder)
    )
    return EtsyOAuthClient(
        client, credentials=EtsyCredentials(keystring=KEYSTRING, shared_secret=SHARED_SECRET)
    )


def test_client__satisfies_the_authorization_server_port() -> None:
    server: EtsyAuthorizationServer = oauth(Recorder())

    assert server is not None


def test_authorization_url__has_every_parameter_with_space_separated_scopes() -> None:
    url = oauth(Recorder()).authorization_url(
        state="fake-state",
        code_challenge="fake-challenge",
        scopes=frozenset({"transactions_r", "shops_r", "listings_r"}),
        redirect_uri=REDIRECT,
    )

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ETSY_AUTHORIZATION_URL
    assert ETSY_AUTHORIZATION_URL == "https://www.etsy.com/oauth/connect"
    assert query == {
        "response_type": ["code"],
        "client_id": [KEYSTRING],
        "redirect_uri": [REDIRECT],
        "scope": ["listings_r shops_r transactions_r"],
        "state": ["fake-state"],
        "code_challenge": ["fake-challenge"],
        "code_challenge_method": ["S256"],
    }
    assert "+" not in parts.query  # spaces are %20, as Etsy's examples show
    assert SHARED_SECRET not in url


async def test_exchange_code__posts_the_form_and_reads_the_grant() -> None:
    recorder = Recorder(httpx.Response(200, json=token_body(scope="shops_r transactions_r")))

    grant = await oauth(recorder).exchange_code(
        code=CODE, code_verifier=VERIFIER, redirect_uri=REDIRECT
    )

    [request] = recorder.requests
    assert str(request.url) == ETSY_TOKEN_URL
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["x-api-key"] == f"{KEYSTRING}:{SHARED_SECRET}"
    assert recorder.form() == {
        "grant_type": "authorization_code",
        "client_id": KEYSTRING,
        "redirect_uri": REDIRECT,
        "code": CODE,
        "code_verifier": VERIFIER,
    }
    assert (grant.access_token, grant.refresh_token) == (ACCESS, REFRESH)
    assert grant.expires_in == timedelta(seconds=3600)
    assert grant.scopes == frozenset({"shops_r", "transactions_r"})
    assert grant.user_id == 12345678


async def test_exchange_code__without_scope_in_the_answer__scopes_unknown() -> None:
    recorder = Recorder(httpx.Response(200, json=token_body()))

    grant = await oauth(recorder).exchange_code(
        code=CODE, code_verifier=VERIFIER, redirect_uri=REDIRECT
    )

    assert grant.scopes is None


async def test_refresh__posts_the_refresh_grant() -> None:
    recorder = Recorder(httpx.Response(200, json=token_body()))

    grant = await oauth(recorder).refresh(REFRESH)

    assert recorder.form() == {
        "grant_type": "refresh_token",
        "client_id": KEYSTRING,
        "refresh_token": REFRESH,
    }
    assert grant.refresh_token == REFRESH


@pytest.mark.parametrize("status", [400, 401])
async def test_token_request__invalid_grant__invalid_grant_error(status: int) -> None:
    body = {"error": "invalid_grant", "error_description": "code has expired"}
    recorder = Recorder(httpx.Response(status, json=body))

    with pytest.raises(OAuthInvalidGrantError):
        await oauth(recorder).refresh(REFRESH)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_request"}),
        httpx.Response(400, text="not json"),
        httpx.Response(429, json={"error": "slow down"}),
        httpx.Response(500, json={"error": "server_error"}),
        httpx.Response(503, text="<html>maintenance</html>"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"access_token": ACCESS}),
        httpx.Response(200, json=token_body(expires_in="soon")),
        httpx.Response(200, json=token_body(access_token="no-user-prefix")),  # noqa: S106 - test value
        httpx.Response(200, json=token_body(token_type="mac")),  # noqa: S106 - test value
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("refused"),
    ],
)
async def test_token_request__any_other_failure__unavailable_once_without_retry(
    response: httpx.Response | Exception,
) -> None:
    recorder = Recorder(response)

    with pytest.raises(OAuthUnavailableError):
        await oauth(recorder).exchange_code(
            code=CODE, code_verifier=VERIFIER, redirect_uri=REDIRECT
        )

    assert len(recorder.requests) == 1


@pytest.mark.parametrize("outcome", ["invalid_grant", "server_error", "ok"])
async def test_token_requests__never_log_or_raise_with_a_token_code_or_secret(
    outcome: str,
) -> None:
    responses = {
        "invalid_grant": httpx.Response(400, json={"error": "invalid_grant"}),
        "server_error": httpx.Response(500, json={"error": "server_error", "access": ACCESS}),
        "ok": httpx.Response(200, json=token_body()),
    }
    secrets = (ACCESS, REFRESH, CODE, VERIFIER, SHARED_SECRET)

    with capture_logs("DEBUG") as logs:
        try:
            grant = await oauth(Recorder(responses[outcome])).exchange_code(
                code=CODE, code_verifier=VERIFIER, redirect_uri=REDIRECT
            )
            text = repr(grant)
        except (OAuthInvalidGrantError, OAuthUnavailableError) as error:
            text = f"{error!s} {error!r} {error.__cause__!r}"

    for secret in secrets:
        assert secret not in text
        assert secret not in logs.raw()


# --- The shop of the connected user (getShopByOwnerUserId) --------------------------------------


def shop_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / "shop.json").read_text(encoding="utf-8"))
    return body


class NoTokens:
    """The vault: never asked, because the shop is not stored yet when it is looked up."""

    async def access_token(self, shop_id: int) -> str:
        raise AssertionError(f"the vault must not be asked for shop {shop_id}")


def adapter_for(recorder: Recorder) -> EtsyAdapter:
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(recorder)
    )
    return EtsyAdapter(
        client,
        credentials=EtsyCredentials(keystring=KEYSTRING, shared_secret=SHARED_SECRET),
        access_tokens=NoTokens(),
    )


async def test_shop_of_user__gets_the_users_shop_with_the_given_token() -> None:
    recorder = Recorder(httpx.Response(200, json=shop_body()))

    adapter = adapter_for(recorder)
    directory: EtsyShopDirectory = adapter

    shop = await directory.shop_of_user(12345678, access_token=ACCESS)

    [request] = recorder.requests
    assert request.url.path == "/v3/application/users/12345678/shops"
    assert request.headers["authorization"] == f"Bearer {ACCESS}"
    assert request.headers["x-api-key"] == f"{KEYSTRING}:{SHARED_SECRET}"
    assert (shop.shop_id, shop.name) == (10000001, "FakeTestShop")


async def test_shop_of_user__user_without_a_shop__not_found() -> None:
    recorder = Recorder(httpx.Response(404, json={"error": "User does not have a shop"}))
    adapter = adapter_for(recorder)

    with pytest.raises(MarketplaceNotFoundError):
        await adapter.shop_of_user(12345678, access_token=ACCESS)


@pytest.mark.parametrize("user_id", [0, -1])
async def test_shop_of_user__non_positive_user__refused_before_any_call(user_id: int) -> None:
    recorder = Recorder()
    adapter = adapter_for(recorder)

    with pytest.raises(ValueError, match="user"):
        await adapter.shop_of_user(user_id, access_token=ACCESS)

    assert recorder.requests == []
