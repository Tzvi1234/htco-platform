"""Property-based checks of the running app against the committed contract (AC-1.3.3).

Schemathesis generates requests from `packages/contracts/openapi.yaml`, including methods the
contract does not allow; every response must match the contract and none may be a server error.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import settings

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import WebAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey
from tests.session_fakes import Auth, Store, Tokens
from tools.openapi import CONTRACT_PATH

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://api.test"

schema = schemathesis.openapi.from_path(REPO_ROOT / CONTRACT_PATH)
# HTTP header values are ASCII; the test client cannot even send other characters. Generated
# bearer tokens are random bytes that the client cannot encode either; without them every
# authenticated operation is still exercised (401 with a problem body, checked against the
# contract), and `tests/api/test_auth.py` covers real and forged tokens.
schema.config.generation.update(codec="ascii", allow_x00=False, with_security_parameters=False)


class _NoMemberships:
    async def memberships_of(
        self, user: AuthenticatedUser
    ) -> Sequence[Membership]:  # pragma: no cover
        raise AssertionError(f"generated tokens are never valid: {user.user_id}")


def _app() -> FastAPI:
    """The app with sign-in configured: every generated token fails verification (401), and
    generated web sign-ins carry no CSRF header (403) or no session cookie (401)."""
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(SigningKey("k1")))
    web = WebAuth(
        auth=Auth(), tokens=Tokens(), store=Store(), allowed_origins=("https://app.test",)
    )
    return create_app(
        AppOptions(authenticator=Authenticator(verifier, _NoMemberships()), web_auth=web)
    )


@pytest.mark.ac("AC-1.3.3")
@schema.parametrize()
@settings(max_examples=50, deadline=None)
def test_api__every_operation__conforms_to_the_contract(
    case: schemathesis.Case[Any],
) -> None:
    # A client per example, closed afterwards. Schemathesis's own ASGI client keeps the app's
    # lifespan running until the interpreter exits, which leaks streams between tests.
    request = case.as_transport_kwargs(base_url=BASE_URL)
    cookies = request.pop("cookies", None) or {}  # httpx takes cookies on the client only
    if isinstance(request.get("data"), str | bytes):  # raw bodies are `content` in httpx
        request["content"] = request.pop("data")
    with TestClient(_app(), base_url=BASE_URL, cookies=cookies) as client:
        response = client.request(**request)

    case.validate_response(response)
