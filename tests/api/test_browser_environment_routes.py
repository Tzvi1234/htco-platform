"""`GET /v1/shops/{shop_id}/browser-environment` (slice 5.16): the status only, for the Owner and
Admins; a shop without an environment is `not_configured`."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.browser_environments import BrowserEnvironmentServices
from htco_application.access.authentication import Membership
from htco_domain.browser import EnvironmentStatus
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("5c000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("5d000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("5c300000-0000-0000-0000-000000000001")
BARE_SHOP = uuid.UUID("5c300000-0000-0000-0000-000000000002")
FOREIGN = uuid.UUID("5d300000-0000-0000-0000-000000000001")


class Reader:
    def __init__(self) -> None:
        self.statuses = {SHOP: EnvironmentStatus.LOGIN_REQUIRED, FOREIGN: EnvironmentStatus.READY}

    def status_for_user(
        self, claims: Mapping[str, Any], shop_id: uuid.UUID
    ) -> EnvironmentStatus | None:
        assert claims
        return self.statuses.get(shop_id)


class Owners:
    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return {SHOP: ORG, BARE_SHOP: ORG, FOREIGN: OTHER_ORG}.get(shop_id)


def client(role: Role = Role.OWNER, *, wired: bool = True) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                verifier, Memberships(Membership(ORG, role, frozenset({SHOP, BARE_SHOP})))
            ),
            browser_environments=BrowserEnvironmentServices(Reader(), Owners()) if wired else None,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    )


def path(shop: uuid.UUID) -> str:
    return f"/v1/shops/{shop}/browser-environment"


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_get__owner_and_admin__see_the_status_only(role: Role) -> None:
    async with client(role) as http:
        answer = await http.get(path(SHOP))

    assert answer.status_code == 200
    assert answer.json() == {"shop_id": str(SHOP), "status": "login_required"}
    assert answer.headers["Cache-Control"] == "no-store"


@pytest.mark.ac("AC-5.16.4")
async def test_get__shop_without_environment__not_configured() -> None:
    async with client() as http:
        answer = await http.get(path(BARE_SHOP))

    assert answer.json()["status"] == "not_configured"


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.VIEWER])
async def test_get__other_roles__403(role: Role) -> None:
    async with client(role) as http:
        answer = await http.get(path(SHOP))

    assert answer.status_code == 403


@pytest.mark.ac("AC-5.16.3")
async def test_get__another_organizations_shop__404() -> None:
    async with client() as http:
        answer = await http.get(path(FOREIGN))

    assert answer.status_code == 404
    assert "ready" not in answer.text


async def test_get__not_wired__503() -> None:
    async with client(wired=False) as http:
        answer = await http.get(path(SHOP))

    assert answer.status_code == 503
    assert answer.json()["type"].endswith("browser-environments-unavailable")


async def test_create__there_is_no_endpoint() -> None:
    async with client() as http:
        answer = await http.post(path(SHOP), json={})

    assert answer.status_code == 405
