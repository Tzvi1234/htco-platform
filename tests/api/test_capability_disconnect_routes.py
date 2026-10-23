"""Capability re-check (slice 4.5) and disconnect (slice 4.7) through the API, with fakes.

`POST /v1/shop-integrations/{shop_id}/capability-checks`: Owner and Admin, from the web app (CSRF),
at most once per 30 seconds per shop. `POST /v1/shop-integrations/{shop_id}/disconnect`: the
Owner with MFA and a recent step-up. The probes also run right after a connection completes.
After a disconnect no marketplace call is made for the shop (AC-4.7.1).
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, override

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.web_auth import WebAuth
from htco_application.access.authentication import Membership
from htco_application.etsy_connect import EtsyConnect
from htco_application.shop_capabilities import CapabilityChecks
from htco_application.shop_integrations import IntegrationSummary
from htco_domain.identity import Role
from htco_domain.integrations import IntegrationStatus
from htco_platform.jwt import JwtVerifier
from tests.api.test_etsy_connect_routes import (
    CREDENTIALS,
    CSRF,
    KEY,
    ORIGIN,
    PROBLEMS,
    Aal2AfterVerify,
    Clock,
    Memberships,
    Mfa,
)
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks
from tests.etsy_fakes import (
    GOOD_CODE,
    NOW,
    REDIRECT_URI,
    AuditLog,
    AuthorizationServer,
    Capabilities,
    Connector,
    Directory,
    Pending,
    Pinning,
    Probe,
    Vault,
)
from tests.session_fakes import Auth, Store

pytestmark = pytest.mark.anyio

CAPABILITIES = [
    "granted_scopes",
    "shop_details",
    "receipts_read",
    "buyer_address",
    "listings_read",
    "webhooks",
    "shipment_write",
]


class Integrations:
    """The shop card's reader over the fake vault and capability store."""

    def __init__(self, vault: Vault, store: Capabilities) -> None:
        self.vault = vault
        self.store = store

    async def list_integrations(
        self, claims: Any, organization_id: uuid.UUID
    ) -> Sequence[IntegrationSummary]:
        assert claims is not None
        return [
            IntegrationSummary.of(row, tuple(self.store.rows.get(row.shop_id, {}).values()))
            for row in self.vault.rows.values()
            if row.organization_id == organization_id
        ]


class Exploding(Probe):
    @override
    async def get_shop(self, shop_id: int, *, urgent: bool = False, tokens: object = None) -> Any:
        raise RuntimeError("probe bug")


@dataclass
class World:
    clock: Clock
    vault: Vault
    probe: Probe
    store: Capabilities
    audit: AuditLog
    http: httpx.AsyncClient
    responses: list[httpx.Response]
    shop_id: str = ""

    async def connect_shop(self) -> dict[str, Any]:
        started = await self.http.post("/v1/shops/etsy/connect", headers=CSRF)
        assert started.status_code == 200, started.text
        state = httpx.URL(started.json()["authorization_url"]).params["state"]
        done = await self.send(
            self.http.post(
                "/v1/integrations/etsy/callback",
                json={"code": GOOD_CODE, "state": state},
                headers=CSRF,
            )
        )
        assert done.status_code == 200, done.text
        body: dict[str, Any] = done.json()
        return body

    async def recheck(self, shop_id: str, headers: dict[str, str] = CSRF) -> httpx.Response:
        return await self.send(
            self.http.post(f"/v1/shop-integrations/{shop_id}/capability-checks", headers=headers)
        )

    async def disconnect(self, shop_id: str, headers: dict[str, str] = CSRF) -> httpx.Response:
        return await self.send(
            self.http.post(f"/v1/shop-integrations/{shop_id}/disconnect", headers=headers)
        )

    async def send(self, pending: Any) -> httpx.Response:
        response: httpx.Response = await pending
        self.responses.append(response)
        return response


class TwoOrganizations(Memberships):
    """The caller owns a second organization too: no default organization."""

    several = False

    @override
    async def memberships_of(self, user: Any) -> Sequence[Membership]:
        first = await super().memberships_of(user)
        if not self.several:
            return first
        return [*first, Membership(uuid.UUID(int=7), Role.OWNER, frozenset())]


async def _world(  # noqa: PLR0913 - one keyword per variation
    *,
    role: Role = Role.OWNER,
    several_organizations: bool = False,
    mfa: bool = True,
    probe: Probe | None = None,
    probes_on: bool = True,
    shop_remover: Any = None,
) -> AsyncIterator[World]:
    clock = Clock()
    vault, audit = Vault(), AuditLog()
    store = Capabilities(vault=vault)
    probe = probe or Probe()
    memberships = TwoOrganizations(Role.OWNER)
    connect = EtsyConnect(
        authorization_server=AuthorizationServer(),
        directory=Directory(),
        pending=Pending(),
        connector=Connector(audit, vault=vault),
        audit=audit,
        redirect_uri=REDIRECT_URI,
    )
    checks = CapabilityChecks(vault=vault, marketplace=probe, store=store, tokens=Pinning())
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY)), memberships
            ),
            web_auth=WebAuth(
                auth=Auth(),
                tokens=Aal2AfterVerify(),
                store=Store(),
                allowed_origins=(ORIGIN,),
                mfa=Mfa(),
            ),
            clock=clock,
            etsy=connect,
            integrations=Integrations(vault, store),
            capability_checks=checks if probes_on else None,
            token_vault=vault,
            shop_remover=shop_remover(vault, audit) if shop_remover is not None else None,
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        world = World(clock, vault, probe, store, audit, http, [])
        assert (await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)).is_success
        await http.post("/v1/auth/mfa/enroll", json={"name": "Phone"}, headers=CSRF)
        verified = await http.post(
            "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "123456"}, headers=CSRF
        )
        assert verified.status_code == 200
        # Connecting names no organization yet: the shop is created with one organization, then
        # the caller becomes an owner of a second one.
        world.shop_id = (await world.connect_shop())["shop"]["id"]
        world.probe.calls.clear()
        world.clock.now = NOW + timedelta(minutes=1)
        memberships.role = role
        memberships.several = several_organizations
        if not mfa:
            # A new sign-in without the second factor: aal1 from here on.
            await http.post("/v1/auth/sign-out", headers=CSRF)
            assert (await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)).is_success
        yield world


@pytest.fixture
async def world() -> AsyncIterator[World]:
    async for created in _world():
        yield created


def shop_of(world: World) -> str:
    return world.shop_id


# --- Probes at connection ----------------------------------------------------------------------


async def test_callback__probes_the_new_shop_and_answers_the_results() -> None:
    async for world in _world():
        [connected] = [r for r in world.responses if r.url.path.endswith("/callback")]
        capabilities = connected.json()["integration"]["capabilities"]
        assert [c["capability"] for c in capabilities] == CAPABILITIES
        assert {c["capability"]: c["state"] for c in capabilities}["shipment_write"] == "unknown"
        assert [r.action for r in world.store.audits] == ["shop.capabilities_checked"]


async def test_callback__a_failing_probe__still_connects_with_no_results() -> None:
    async for world in _world(probe=Exploding()):
        [connected] = [r for r in world.responses if r.url.path.endswith("/callback")]
        assert connected.status_code == 200
        assert connected.json()["integration"]["capabilities"] == []


# --- Re-check ----------------------------------------------------------------------------------


@pytest.mark.ac("AC-4.5.1")
async def test_recheck__owner__probes_and_the_card_shows_results_with_checked_at(
    world: World,
) -> None:
    response = await world.recheck(shop_of(world))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["shop_id"] == shop_of(world)
    assert [c["capability"] for c in body["capabilities"]] == CAPABILITIES
    assert all(c["checked_at"].startswith("2026-10-15T09:01") for c in body["capabilities"])
    assert len(world.probe.calls) == 3
    card = (await world.http.get("/v1/shop-integrations")).json()["items"][0]
    assert card["capabilities"] == body["capabilities"]
    assert card["last_successful_call_at"] is not None


async def test_recheck__admin__allowed() -> None:
    async for world in _world(role=Role.ADMIN):
        assert (await world.recheck(shop_of(world))).status_code == 200


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.VIEWER])
async def test_recheck__other_roles__403_without_probing(role: Role) -> None:
    async for world in _world(role=role):
        response = await world.recheck(shop_of(world))

        assert response.status_code == 403
        assert response.json()["type"] == PROBLEMS + "forbidden"
        assert world.probe.calls == []


async def test_recheck__without_csrf__403_without_probing(world: World) -> None:
    response = await world.recheck(shop_of(world), headers={})

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert world.probe.calls == []


async def test_recheck__unknown_shop__404(world: World) -> None:
    response = await world.recheck(str(uuid.uuid4()))

    assert response.status_code == 404


async def test_recheck__twice_at_once__429_with_retry_after(world: World) -> None:
    await world.recheck(shop_of(world))

    again = await world.recheck(shop_of(world))

    assert again.status_code == 429
    assert again.json()["type"] == PROBLEMS + "capability-check-too-soon"
    assert again.headers["Retry-After"] == "30"


async def test_recheck__probes_not_configured__503() -> None:
    async for world in _world(probes_on=False):
        response = await world.recheck(shop_of(world))

        assert response.status_code == 503
        assert response.json()["type"] == PROBLEMS + "etsy-unavailable"


# --- Disconnect --------------------------------------------------------------------------------


async def test_disconnect__owner_with_step_up__disconnected_with_purge_date(world: World) -> None:
    response = await world.disconnect(shop_of(world))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "disconnected"
    assert body["token_health"] == "disconnected"  # noqa: S105 - a status
    assert body["purge_after"].startswith("2026-11-14T09:01")
    assert (body["access_expires_at"], body["refresh_expires_at"]) == (None, None)
    [record] = [r for r in world.vault.audit if r.action == "shop.disconnected"]
    assert record.after is not None
    assert record.after["etsy_revocation"] == "not_offered_by_etsy"


@pytest.mark.ac("AC-4.7.1")
async def test_disconnect__afterwards_no_marketplace_call_for_the_shop(world: World) -> None:
    await world.disconnect(shop_of(world))
    world.clock.now += timedelta(minutes=5)

    response = await world.recheck(shop_of(world))

    assert response.status_code == 409
    assert response.json()["type"] == PROBLEMS + "shop-not-connected"
    assert response.json()["reason"] == "disconnected"
    assert world.probe.calls == []


async def test_recheck__needs_reauth__409_with_the_reason_and_no_probe(world: World) -> None:
    shop = uuid.UUID(shop_of(world))
    world.vault.rows[shop] = replace(world.vault.rows[shop], status=IntegrationStatus.NEEDS_REAUTH)

    response = await world.recheck(shop_of(world))

    assert response.status_code == 409
    assert response.json()["reason"] == "needs_reauth"
    assert world.probe.calls == []


async def test_disconnect__twice__200_and_one_audit(world: World) -> None:
    await world.disconnect(shop_of(world))

    again = await world.disconnect(shop_of(world))

    assert again.status_code == 200
    assert [r.action for r in world.vault.audit].count("shop.disconnected") == 1


async def test_disconnect__step_up_older_than_ten_minutes__403_step_up_required(
    world: World,
) -> None:
    world.clock.now = NOW + timedelta(minutes=11)

    response = await world.disconnect(shop_of(world))

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "step-up-required"
    assert world.vault.rows[uuid.UUID(shop_of(world))].tokens is not None


async def test_disconnect__without_mfa__403_mfa_required() -> None:
    async for world in _world(mfa=False):
        response = await world.disconnect(shop_of(world))

        assert response.status_code == 403
        assert response.json()["type"] == PROBLEMS + "mfa-required"


async def test_disconnect__admin__403_forbidden() -> None:
    async for world in _world(role=Role.ADMIN):
        response = await world.disconnect(shop_of(world))

        assert response.status_code == 403
        assert response.json()["type"] == PROBLEMS + "forbidden"


async def test_disconnect__without_csrf__403_and_still_connected(world: World) -> None:
    response = await world.disconnect(shop_of(world), headers={})

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert world.vault.rows[uuid.UUID(shop_of(world))].tokens is not None


async def test_disconnect__unknown_shop__404(world: World) -> None:
    assert (await world.disconnect(str(uuid.uuid4()))).status_code == 404


async def test_no_response__ever_carries_a_token(world: World) -> None:
    await world.recheck(shop_of(world))
    await world.disconnect(shop_of(world))

    for response in world.responses:
        assert "fake-access" not in response.text
        assert "fake-refresh" not in response.text


async def test_disconnect__owner_of_several_organizations__authorized_for_the_shops_own() -> None:
    """Codex review of #49: the route must not need a default organization."""
    async for world in _world(several_organizations=True):
        response = await world.disconnect(shop_of(world))

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "disconnected"
