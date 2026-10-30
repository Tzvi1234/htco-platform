"""The Etsy connect endpoints (slices 4.3 and 4.4) through the API, with in-memory fakes.

`POST /v1/shops/etsy/connect` needs the Owner, a second factor and a recent step-up, from the web
app (CSRF); it answers the authorization URL. Etsy sends the browser back to
`GET /v1/integrations/etsy/callback`, which seals the code and state into an `HttpOnly` cookie and
redirects to the web app's clean finishing page; its `POST /v1/integrations/etsy/callback` (no
body) completes the connection for the same web session (Codex security review, owner gate v4).
`GET /v1/shop-integrations` shows status, scopes and token
health to Owner and Admin. No response ever carries a token (AC-4.2.3).
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, override
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.sealed_cookies import ETSY_CALLBACK_COOKIE
from htco_api.web_auth import WebAuth
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_application.etsy_connect import EtsyConnect
from htco_application.marketplace import QuotaSnapshot, QuotaStatus
from htco_application.mfa import Enrollment, Factor
from htco_application.shop_integrations import IntegrationSummary
from htco_application.web_sessions import CredentialsRefusedError, IssuedTokens
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.integrations import IntegrationProvider, IntegrationStatus
from htco_platform.crypto import Sealer
from htco_platform.jwt import JwtVerifier
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey, claims
from tests.etsy_fakes import (
    ETSY_SHOP,
    GOOD_CODE,
    REDIRECT_URI,
    AuditLog,
    AuthorizationServer,
    Connector,
    Directory,
    Pending,
)
from tests.session_fakes import NOW, USER, Auth, Store, Tokens, tokens

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
PROBLEMS = "https://ht-co.app/problems/"
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
CSRF = {"X-HTCO-CSRF": "1", "Origin": ORIGIN}
CREDENTIALS = {"email": "owner@example.test", "password": "correct horse battery"}
KEY = SigningKey("k1")


class Mfa:
    def __init__(self) -> None:
        self.factors: list[Factor] = []

    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> Enrollment:  # noqa: ARG002
        self.factors.append(Factor("f1", verified=False, name=friendly_name))
        return Enrollment("f1", "data:image/svg+xml;utf-8,<svg/>", "SECRET", "otpauth://totp/x")

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> IssuedTokens:  # noqa: ARG002
        if code != "123456":
            raise CredentialsRefusedError
        self.factors = [replace(f, verified=True) for f in self.factors]
        return tokens(50)

    async def factors_of(self, access_token: str) -> list[Factor]:  # noqa: ARG002 - fake
        return list(self.factors)

    async def remove_factor(self, access_token: str, factor_id: str) -> None:  # noqa: ARG002
        self.factors = [f for f in self.factors if f.id != factor_id]


class Aal2AfterVerify(Tokens):
    @override
    async def read(self, access_token: str) -> AuthenticatedUser:
        aal = AssuranceLevel.AAL2 if access_token == "access-50" else AssuranceLevel.AAL1  # noqa: S105
        return AuthenticatedUser(user_id=USER, assurance=aal, session_id=None, claims={})


class Memberships:
    def __init__(self, role: Role = Role.OWNER) -> None:
        self.role = role

    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:  # noqa: ARG002
        return [Membership(ORG, self.role, frozenset())]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Integrations:
    def __init__(self, connector: Connector) -> None:
        self.connector = connector
        self.asked: list[uuid.UUID] = []

    async def list_integrations(
        self, claims: Any, organization_id: uuid.UUID
    ) -> Sequence[IntegrationSummary]:
        assert claims is not None
        self.asked.append(organization_id)
        return [
            IntegrationSummary.of(i)
            for i in self.connector.integrations.values()
            if i.organization_id == organization_id
        ]


@dataclass
class World:
    clock: Clock
    server: AuthorizationServer
    directory: Directory
    pending: Pending
    audit: AuditLog
    connector: Connector
    memberships: Memberships
    http: httpx.AsyncClient
    responses: list[httpx.Response]

    async def connect(self) -> httpx.Response:
        return await self._send(self.http.post("/v1/shops/etsy/connect", headers=CSRF))

    async def callback(self, query: dict[str, Any], **headers: str) -> httpx.Response:
        """Etsy's redirect (a top-level GET, no CSRF header), then the finishing page's POST."""
        await self.redirect(query)
        return await self._send(
            self.http.post("/v1/integrations/etsy/callback", headers=CSRF | headers)
        )

    async def redirect(self, query: dict[str, Any]) -> httpx.Response:
        return await self.http.get("/v1/integrations/etsy/callback", params=query)

    async def integrations(self) -> httpx.Response:
        return await self._send(self.http.get("/v1/shop-integrations"))

    async def state(self) -> str:
        response = await self.connect()
        assert response.status_code == 200, response.text
        url: str = response.json()["authorization_url"]
        [state] = parse_qs(urlsplit(url).query)["state"]
        return state

    async def _send(self, pending: Any) -> httpx.Response:
        response: httpx.Response = await pending
        self.responses.append(response)
        return response


async def _world(*, etsy: bool = True, role: Role = Role.OWNER) -> AsyncIterator[World]:
    clock = Clock()
    server, directory, pending, audit = AuthorizationServer(), Directory(), Pending(), AuditLog()
    connector = Connector(audit)
    memberships = Memberships(role)
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    web = WebAuth(
        auth=Auth(),
        tokens=Aal2AfterVerify(),
        store=Store(),
        allowed_origins=(ORIGIN,),
        mfa=Mfa(),
        link_sealer=Sealer(b"k" * 32),
    )
    connect = EtsyConnect(
        authorization_server=server,
        directory=directory,
        pending=pending,
        connector=connector,
        audit=audit,
        redirect_uri=REDIRECT_URI,
    )
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, memberships),
            web_auth=web,
            clock=clock,
            etsy=connect if etsy else None,
            integrations=Integrations(connector),
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as http:
        responses: list[httpx.Response] = []
        world = World(
            clock, server, directory, pending, audit, connector, memberships, http, responses
        )
        signed_in = await http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)
        assert signed_in.status_code == 200
        yield world


async def _with_mfa(world: World) -> World:
    """Enroll and verify the authenticator app: aal2 and a fresh step-up for ten minutes."""
    await world.http.post("/v1/auth/mfa/enroll", json={"name": "Phone"}, headers=CSRF)
    verified = await world.http.post(
        "/v1/auth/mfa/verify", json={"factor_id": "f1", "code": "123456"}, headers=CSRF
    )
    assert verified.status_code == 200
    return world


@pytest.fixture
async def world() -> AsyncIterator[World]:
    async for created in _world():
        yield await _with_mfa(created)


@pytest.fixture
async def aal1() -> AsyncIterator[World]:
    async for created in _world():
        yield created


# --- Connect -----------------------------------------------------------------------------------


async def test_connect__owner_with_mfa_and_step_up__authorization_url_not_cached(
    world: World,
) -> None:
    response = await world.connect()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    url = response.json()["authorization_url"]
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://www.etsy.com/oauth/connect?")
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert set(response.json()) == {"authorization_url"}
    [pending] = world.pending.rows.values()
    assert (pending.organization_id, pending.user_id) == (ORG, USER)


async def test_connect__without_mfa__403_mfa_required(aal1: World) -> None:
    response = await aal1.connect()

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "mfa-required"
    assert aal1.pending.rows == {}


async def test_connect__step_up_lapsed__403_step_up_required(world: World) -> None:
    world.clock.now = NOW + timedelta(minutes=11)

    response = await world.connect()

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "step-up-required"


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.VIEWER])
async def test_connect__not_the_owner__403_forbidden(world: World, role: Role) -> None:
    world.memberships.role = role

    response = await world.connect()

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "forbidden"


async def test_connect__without_the_csrf_header__403_csrf_failed(world: World) -> None:
    response = await world.http.post("/v1/shops/etsy/connect", headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert world.pending.rows == {}


async def test_connect__from_another_site__403_csrf_failed(world: World) -> None:
    headers = {"X-HTCO-CSRF": "1", "Origin": "https://evil.example"}

    response = await world.http.post("/v1/shops/etsy/connect", headers=headers)

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"


async def test_connect__not_signed_in__401() -> None:
    app = create_app(AppOptions())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.test"
    ) as http:
        response = await http.post("/v1/shops/etsy/connect", headers=CSRF)

    assert response.status_code == 401


async def test_connect__integration_off__503_etsy_unavailable() -> None:
    async for created in _world(etsy=False):
        world = await _with_mfa(created)

        connect = await world.connect()
        callback = await world.callback({"code": GOOD_CODE, "state": "x"})

        for response in (connect, callback):
            assert response.status_code == 503
            body = response.json()
            assert body["type"] == PROBLEMS + "etsy-unavailable"
            assert body["title"] == "Etsy connection unavailable"


async def test_connect__bearer_token__refused_because_step_up_needs_the_web_session() -> None:
    async for world in _world():
        token = KEY.sign(claims(aal="aal2"))
        async with httpx.AsyncClient(
            transport=world.http._transport,
            base_url="https://api.test",
            headers={"Authorization": f"Bearer {token}"},
        ) as bearer:
            connect = await bearer.post("/v1/shops/etsy/connect")
            callback = await bearer.post("/v1/integrations/etsy/callback")

        assert connect.json()["type"] == PROBLEMS + "step-up-required"
        assert callback.status_code == 403
        assert callback.json()["type"] == PROBLEMS + "web-session-required"


# --- Callback ----------------------------------------------------------------------------------


@pytest.mark.ac("AC-4.3.3")
async def test_callback__connects_the_shop_and_answers_the_shop_view(world: World) -> None:
    state = await world.state()

    response = await world.callback({"code": GOOD_CODE, "state": state})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["reconnected"] is False
    assert body["shop"]["name"] == "FakeCraftsShop"
    assert body["shop"]["organization_id"] == str(ORG)
    assert body["shop"]["status"] == "active"
    integration = body["integration"]
    assert integration["shop_id"] == body["shop"]["id"]
    assert integration["provider"] == "etsy"
    assert integration["status"] == "connected"
    assert integration["etsy_shop_id"] == ETSY_SHOP
    assert integration["scopes"] == ["listings_r", "shops_r", "transactions_r"]
    assert integration["token_health"] == "ok"  # noqa: S105 - test value
    assert world.connector.grants == [(uuid.UUID(body["shop"]["id"]), USER)]
    assert [r.action for r in world.audit.records] == ["shop.connected"]


@pytest.mark.ac("AC-4.3.2")
async def test_callback__same_shop_twice__one_shop_reconnected(world: World) -> None:
    first = await world.callback({"code": GOOD_CODE, "state": await world.state()})
    second = await world.callback({"code": GOOD_CODE, "state": await world.state()})

    assert first.json()["shop"]["id"] == second.json()["shop"]["id"]
    assert second.json()["reconnected"] is True
    assert len(world.connector.shops) == 1


@pytest.mark.ac("AC-4.3.4")
async def test_callback__reconnect_with_more_scopes__scopes_updated(world: World) -> None:
    world.server.scopes = frozenset({"shops_r"})
    await world.callback({"code": GOOD_CODE, "state": await world.state()})
    world.server.scopes = frozenset({"shops_r", "transactions_r", "listings_r", "listings_w"})

    second = await world.callback({"code": GOOD_CODE, "state": await world.state()})

    assert second.json()["integration"]["scopes"] == [
        "listings_r",
        "listings_w",
        "shops_r",
        "transactions_r",
    ]
    listed = (await world.integrations()).json()["items"]
    assert [item["scopes"] for item in listed] == [second.json()["integration"]["scopes"]]


@pytest.mark.ac("AC-4.3.1")
@pytest.mark.parametrize(
    "body",
    [{"code": GOOD_CODE}, {"code": GOOD_CODE, "state": ""}, {"code": GOOD_CODE, "state": " "}],
)
async def test_callback__missing_state__400_rejected_and_audited(
    world: World, body: dict[str, str]
) -> None:
    response = await world.callback(body)

    assert response.status_code == 400
    assert response.json()["type"] == PROBLEMS + "etsy-state-rejected"
    assert world.server.exchanged == []
    assert [r.action for r in world.audit.records] == ["shop.connect_rejected"]


@pytest.mark.ac("AC-4.3.1")
async def test_callback__reused_state__400_rejected_and_audited(world: World) -> None:
    state = await world.state()
    await world.callback({"code": GOOD_CODE, "state": state})

    again = await world.callback({"code": GOOD_CODE, "state": state})

    assert again.status_code == 400
    assert again.json()["type"] == PROBLEMS + "etsy-state-rejected"
    assert [r.action for r in world.audit.records] == ["shop.connected", "shop.connect_rejected"]


@pytest.mark.ac("AC-4.3.1")
async def test_callback__expired_state__400_rejected(world: World) -> None:
    state = await world.state()
    world.clock.now = NOW + timedelta(minutes=10)

    response = await world.callback({"code": GOOD_CODE, "state": state})

    assert response.json()["type"] == PROBLEMS + "etsy-state-rejected"
    assert world.server.exchanged == []


@pytest.mark.ac("AC-4.3.1")
async def test_callback__state_from_another_browser_session__400_rejected(world: World) -> None:
    state = await world.state()
    # A second sign-in of the same user replaces this browser's cookie: a new web session.
    await world.http.post("/v1/auth/sign-in", json=CREDENTIALS, headers=CSRF)
    await _with_mfa(world)

    response = await world.callback({"code": GOOD_CODE, "state": state})

    assert response.status_code == 400
    assert response.json()["type"] == PROBLEMS + "etsy-state-rejected"
    assert world.server.exchanged == []


async def test_callback__without_csrf__403_and_the_state_stays_usable(world: World) -> None:
    state = await world.state()

    await world.redirect({"code": GOOD_CODE, "state": state})
    refused = await world.http.post(
        "/v1/integrations/etsy/callback", headers={"Origin": "https://evil.example"}
    )
    accepted = await world._send(world.http.post("/v1/integrations/etsy/callback", headers=CSRF))

    assert refused.status_code == 403
    assert refused.json()["type"] == PROBLEMS + "csrf-failed"
    assert accepted.status_code == 200


async def test_redirect__seals_code_and_state_in_an_httponly_cookie_and_cleans_the_url(
    world: World,
) -> None:
    state = await world.state()

    response = await world.redirect({"code": GOOD_CODE, "state": state})

    assert response.status_code == 303
    assert response.headers["location"] == "/integrations/etsy/callback"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{ETSY_CALLBACK_COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/", "Max-Age=600"):
        assert attribute.lower() in cookie.lower()
    assert GOOD_CODE not in cookie
    assert state not in cookie
    assert world.server.exchanged == []  # nothing is redeemed without the session


@pytest.mark.parametrize(
    ("query", "outcome"),
    [
        ({"error": "access_denied", "state": "x"}, "cancelled"),
        ({"error": "server_error"}, "failed"),
        ({}, "incomplete"),
        ({"code": "", "state": "x"}, "incomplete"),
    ],
    ids=["declined", "etsy-error", "nothing", "empty-code"],
)
async def test_redirect__without_a_code__to_the_page_with_its_outcome_and_no_cookie(
    world: World, query: dict[str, str], outcome: str
) -> None:
    response = await world.redirect(query)

    assert response.status_code == 303
    assert response.headers["location"] == f"/integrations/etsy/callback?outcome={outcome}"
    assert "set-cookie" not in response.headers


async def test_redirect__oversized_code__422(world: World) -> None:
    response = await world.redirect({"code": "c" * 2049, "state": "x"})

    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"


async def test_callback__without_the_cookie__400_rejected_and_audited(world: World) -> None:
    await world.state()

    response = await world._send(world.http.post("/v1/integrations/etsy/callback", headers=CSRF))

    assert response.status_code == 400
    assert response.json()["type"] == PROBLEMS + "etsy-state-rejected"
    assert world.server.exchanged == []
    assert [r.action for r in world.audit.records] == ["shop.connect_rejected"]


async def test_callback__the_cookie_is_spent_after_one_try(world: World) -> None:
    first = await world.callback({"code": GOOD_CODE, "state": await world.state()})
    second = await world._send(world.http.post("/v1/integrations/etsy/callback", headers=CSRF))

    assert first.status_code == 200
    assert "max-age=0" in first.headers["set-cookie"].lower()
    assert second.json()["type"] == PROBLEMS + "etsy-state-rejected"


async def test_callback__etsy_refuses_the_code__400_authorization_refused(world: World) -> None:
    response = await world.callback({"code": "bad", "state": await world.state()})

    assert response.status_code == 400
    assert response.json()["type"] == PROBLEMS + "etsy-authorization-refused"


async def test_callback__etsy_unreachable__502(world: World) -> None:
    world.server.exchange_outcome = "unavailable"

    response = await world.callback({"code": GOOD_CODE, "state": await world.state()})

    assert response.status_code == 502
    assert response.json()["type"] == PROBLEMS + "etsy-unreachable"


async def test_callback__account_without_a_shop__409_no_shop(world: World) -> None:
    world.directory.shops = {}

    response = await world.callback({"code": GOOD_CODE, "state": await world.state()})

    assert response.status_code == 409
    assert response.json()["type"] == PROBLEMS + "etsy-no-shop"


async def test_callback__shop_connected_in_another_organization__409(world: World) -> None:
    await world.callback({"code": GOOD_CODE, "state": await world.state()})
    world.connector.integrations = {
        k: replace(v, organization_id=uuid.uuid4()) for k, v in world.connector.integrations.items()
    }

    response = await world.callback({"code": GOOD_CODE, "state": await world.state()})

    assert response.status_code == 409
    assert response.json()["type"] == PROBLEMS + "etsy-shop-connected-elsewhere"


async def test_callback__caller_demoted_since_the_start__403_forbidden(world: World) -> None:
    state = await world.state()
    world.memberships.role = Role.ADMIN

    response = await world.callback({"code": GOOD_CODE, "state": state})

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "forbidden"
    assert world.server.exchanged == []


# --- Integrations list -------------------------------------------------------------------------


async def test_integrations__owner_sees_status_scopes_and_token_health(world: World) -> None:
    await world.callback({"code": GOOD_CODE, "state": await world.state()})
    # The refresh token ends in nine days (the session would not last 81 days to see it).
    world.connector.integrations = {
        k: replace(v, tokens=replace(v.tokens, refresh_expires_at=NOW + timedelta(days=9)))
        for k, v in world.connector.integrations.items()
        if v.tokens is not None
    }

    response = await world.integrations()

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert item["status"] == "connected"
    # The refresh token ends in under ten days.
    assert item["token_health"] == "expiring"  # noqa: S105 - a status
    assert item["refresh_expires_at"].startswith("2026-10-24")
    assert item["failure_count"] == 0
    assert set(item) == {
        "shop_id",
        "provider",
        "status",
        "etsy_shop_id",
        "scopes",
        "token_health",
        "access_expires_at",
        "refresh_expires_at",
        "failure_count",
        "last_successful_call_at",
        "connected_at",
        "updated_at",
        # Slices 4.5 and 4.7 (additive): probe results and the disconnect dates.
        "capabilities",
        "disconnected_at",
        "purge_after",
    }
    assert (item["capabilities"], item["disconnected_at"], item["purge_after"]) == ([], None, None)


async def test_integrations__needs_reauth_is_shown(world: World) -> None:
    await world.callback({"code": GOOD_CODE, "state": await world.state()})
    world.connector.integrations = {
        k: replace(v, status=IntegrationStatus.NEEDS_REAUTH)
        for k, v in world.connector.integrations.items()
    }

    [item] = (await world.integrations()).json()["items"]

    assert (item["status"], item["token_health"]) == ("needs_reauth", "needs_reauth")


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.VIEWER])
async def test_integrations__roles_below_admin__403_forbidden(world: World, role: Role) -> None:
    world.memberships.role = role

    response = await world.integrations()

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "forbidden"


async def test_integrations__admin_without_mfa_may_look(aal1: World) -> None:
    aal1.memberships.role = Role.ADMIN

    response = await aal1.integrations()

    assert response.status_code == 200
    assert response.json() == {"items": [], "etsy_quota": None}


class Quota:
    def __init__(self, snapshot: QuotaSnapshot | None) -> None:
        self.snapshot = snapshot

    def quota_status(self) -> QuotaStatus:
        return QuotaStatus(snapshot=self.snapshot, floor=500)


def _quota_client(quota: Quota, role: Role = Role.OWNER) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships(role)),
            integrations=Integrations(Connector(AuditLog())),
            etsy_quota=quota,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(aal='aal2'))}"},
    )


@pytest.mark.ac("AC-4.1.2")
@pytest.mark.parametrize(("remaining", "below"), [(420, True), (9_000, False)])
async def test_integrations__the_shop_card_shows_the_remaining_daily_quota(
    remaining: int, below: bool
) -> None:
    snapshot = QuotaSnapshot(
        limit_per_second=10, remaining_this_second=9, limit_per_day=10_000,
        remaining_today=remaining, observed_at=NOW,
    )  # fmt: skip
    async with _quota_client(Quota(snapshot)) as http:
        response = await http.get("/v1/shop-integrations")

    assert response.status_code == 200, response.text
    assert response.json()["etsy_quota"] == {
        "limit_per_day": 10_000,
        "remaining_today": remaining,
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        "floor": 500,
        "below_floor": below,
    }


async def test_integrations__no_etsy_call_yet__the_floor_and_no_budget() -> None:
    async with _quota_client(Quota(None)) as http:
        body = (await http.get("/v1/shop-integrations")).json()

    assert body["etsy_quota"] == {
        "limit_per_day": None,
        "remaining_today": None,
        "observed_at": None,
        "floor": 500,
        "below_floor": False,
    }


async def test_integrations__the_quota_is_for_the_owner_and_admins_only() -> None:
    async with _quota_client(Quota(None), Role.OPERATIONS) as http:
        response = await http.get("/v1/shop-integrations")

    assert response.status_code == 403
    assert "etsy_quota" not in response.text


async def test_integrations__without_a_reader__503_not_ready() -> None:
    app = create_app(AppOptions())
    token = KEY.sign(claims(aal="aal2"))
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app.state.authenticator = Authenticator(verifier, Memberships())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        response = await http.get("/v1/shop-integrations")

    assert response.status_code == 503
    assert response.json()["type"] == PROBLEMS + "not-ready"


# --- No token in any response (AC-4.2.3) -------------------------------------------------------


@pytest.mark.ac("AC-4.2.3")
async def test_no_endpoint_response__ever_contains_a_token_code_or_verifier(world: World) -> None:
    state = await world.state()
    verifier = next(iter(world.pending.rows.values())).code_verifier
    await world.callback({"code": GOOD_CODE, "state": state})
    await world.callback({"code": GOOD_CODE, "state": state})  # rejected
    await world.callback({"code": "bad", "state": await world.state()})
    await world.integrations()
    await world.http.get("/v1/shops")
    secrets = [verifier]
    for integration in world.connector.integrations.values():
        assert integration.tokens is not None
        secrets += [integration.tokens.access_token, integration.tokens.refresh_token]
    secrets += [request.tokens.refresh_token for request in world.connector.requests]

    assert len(world.responses) >= 5
    for response in world.responses:
        seen = response.text + repr(dict(response.headers))
        for secret in secrets:
            assert secret not in seen
    assert IntegrationProvider.ETSY.value in world.responses[1].text
