"""`GET /v1/audit-log` (AC-3.9.1): the Owner's and Admins' audit view, filtered and cursor-paged,
with sensitive values redacted (invented values only)."""

import uuid
from datetime import timedelta

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_application.access.authentication import Membership
from htco_application.audit_log import REDACTED
from htco_domain.actors import ActorKind
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.application.test_audit_log import NOW, MemoryAuditLog, entry
from tests.application.test_shop_dashboard import ORG, SHOP
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims

pytestmark = pytest.mark.anyio


def client(
    role: Role = Role.OWNER, *, log: MemoryAuditLog | None = None, wired: bool = True
) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                verifier, Memberships(Membership(ORG, role, frozenset({SHOP})))
            ),
            audit_log=(log if log is not None else MemoryAuditLog()) if wired else None,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    )


@pytest.mark.ac("AC-3.9.1")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_audit_log__the_owner_and_admins_see_each_change(role: Role) -> None:
    log = MemoryAuditLog([entry(1), entry(2, after={"role": "admin", "email": "a@example.test"})])
    async with client(role, log=log) as http:
        answer = await http.get("/v1/audit-log")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [item["entity_id"] for item in body["items"]] == [
        str(uuid.UUID(int=2)),
        str(uuid.UUID(int=1)),
    ]
    first = body["items"][0]
    assert first["action"] == "membership.role_changed"
    assert first["actor_kind"] == "user"
    assert first["after"] == {"role": "admin", "email": REDACTED}
    assert "example.test" not in answer.text
    assert body["next_cursor"] is None
    assert answer.headers["Cache-Control"] == "no-store"


@pytest.mark.ac("AC-3.9.2")
@pytest.mark.parametrize(
    "role", [Role.DEVELOPER, Role.OPERATIONS, Role.FINANCE, Role.SUPPORT, Role.VIEWER]
)
async def test_audit_log__other_roles__403(role: Role) -> None:
    log = MemoryAuditLog([entry(1)])
    async with client(role, log=log) as http:
        answer = await http.get("/v1/audit-log")

    assert answer.status_code == 403
    assert log.queries == []


async def test_audit_log__every_filter_reaches_the_reader() -> None:
    log = MemoryAuditLog()
    since, until = NOW - timedelta(days=7), NOW
    async with client(log=log) as http:
        answer = await http.get(
            "/v1/audit-log",
            params={
                "shop_id": str(SHOP),
                "actor_kind": "automation",
                "actor_id": "tracking-sync",
                "action": "shop.*",
                "since": since.isoformat(),
                "until": until.isoformat(),
                "limit": 20,
            },
        )

    assert answer.status_code == 200, answer.text
    [query] = log.queries
    assert (query.organization_id, query.shop_id) == (ORG, SHOP)
    assert (query.actor_kind, query.actor_id) == (ActorKind.AUTOMATION, "tracking-sync")
    assert (query.action, query.since, query.until, query.limit) == ("shop.*", since, until, 20)
    assert answer.json() == {"items": [], "next_cursor": None}


async def test_audit_log__pages_with_the_cursor() -> None:
    log = MemoryAuditLog([entry(n) for n in range(1, 4)])
    async with client(log=log) as http:
        first = (await http.get("/v1/audit-log", params={"limit": 2})).json()
        second = (
            await http.get("/v1/audit-log", params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()

    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    assert [i["entity_id"] for i in second["items"]] == [str(uuid.UUID(int=1))]
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"action": "DROP TABLE"},
        {"action": "shop.*.*"},
        {"actor_kind": "robot"},
        {"since": "2026-10-28T12:00:00"},  # no time zone
        {"since": "2026-10-28T12:00:00Z", "until": "2026-10-28T11:00:00Z"},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-a-cursor"},
    ],
)
async def test_audit_log__invalid_filters__422(params: dict[str, str | int]) -> None:
    log = MemoryAuditLog()
    async with client(log=log) as http:
        answer = await http.get("/v1/audit-log", params=params)

    assert answer.status_code == 422, answer.text
    assert log.queries == []


async def test_audit_log__another_organization__404() -> None:
    async with client() as http:
        answer = await http.get("/v1/audit-log", params={"organization_id": str(uuid.uuid4())})

    assert answer.status_code == 404


async def test_audit_log__not_wired__503() -> None:
    async with client(wired=False) as http:
        answer = await http.get("/v1/audit-log")

    assert answer.status_code == 503
    assert answer.json()["type"].endswith("/audit-unavailable")
