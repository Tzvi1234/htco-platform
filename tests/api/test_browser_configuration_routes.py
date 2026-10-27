"""`GET` and `PUT /v1/shops/{shop_id}/browser-environment/configuration` (slice 8A.2b,
AC-8A.2.10): the Owner or an Admin with MFA and a step-up saves a new version; each refusal is
named as codes only and nothing is saved; a stale version is a 409; other roles are refused."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator, require_auth
from htco_api.browser_environments import BrowserEnvironmentServices
from htco_application.access.authentication import AuthContext, Membership
from htco_application.browser_configuration import (
    ConfigurationServices,
    ConfiguredEnvironment,
    MemoryConfigurationStore,
    ShopLanguagesUnavailableError,
)
from htco_domain.browser import EnvironmentStatus
from htco_domain.identity import AssuranceLevel, Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, USER_ID, FakeJwks, claims

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("c3a00000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("c3b00000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("c3a10000-0000-0000-0000-000000000001")
BARE_SHOP = uuid.UUID("c3a10000-0000-0000-0000-000000000002")
FOREIGN = uuid.UUID("c3b10000-0000-0000-0000-000000000001")
ENV = uuid.UUID("c3a20000-0000-0000-0000-000000000001")
PROBLEMS = "https://ht-co.app/problems/"


class Reader:
    def status_for_user(
        self,
        claims: Mapping[str, Any],  # noqa: ARG002
        shop_id: uuid.UUID,  # noqa: ARG002
    ) -> EnvironmentStatus | None:  # pragma: no cover - not used here
        return None


class Owners:
    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:  # noqa: ARG002  # pragma: no cover
        return None


class Languages:
    def __init__(self) -> None:
        self.fail = False

    async def recorded(self, auth: AuthContext, shop_id: uuid.UUID) -> tuple[str, ...]:  # noqa: ARG002
        if self.fail:
            raise ShopLanguagesUnavailableError
        return ("he-IL", "en-US")


def person(
    role: Role = Role.OWNER,
    *,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    stepped_up: bool = True,
) -> AuthContext:
    return AuthContext(
        user_id=USER_ID, assurance=assurance, session_id="s-1",
        memberships=(Membership(ORG, role, frozenset({SHOP})),),
        reauthenticated_at=NOW - timedelta(minutes=2) if stepped_up else None,
    )  # fmt: skip


class World:
    def __init__(self) -> None:
        self.store = MemoryConfigurationStore(
            shops={SHOP: ORG, BARE_SHOP: ORG, FOREIGN: OTHER_ORG},
            environments={SHOP: ConfiguredEnvironment(ENV, ORG, SHOP, EnvironmentStatus.READY)},
        )
        self.languages = Languages()

    def client(
        self, auth: AuthContext | None = None, *, configured: bool = True
    ) -> httpx.AsyncClient:
        verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
        configuration = ConfigurationServices(self.store, self.languages)
        app = create_app(
            AppOptions(
                authenticator=Authenticator(
                    verifier, Memberships(Membership(ORG, Role.OWNER, frozenset()))
                ),
                browser_environments=BrowserEnvironmentServices(
                    Reader(), Owners(), configuration=configuration if configured else None
                ),
                clock=lambda: NOW,
            )
        )
        if auth is not None:
            app.dependency_overrides[require_auth] = lambda: auth
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://api.test",
            headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
        )


def path(shop: uuid.UUID = SHOP) -> str:
    return f"/v1/shops/{shop}/browser-environment/configuration"


@pytest.fixture
def world() -> World:
    return World()


@pytest.mark.ac("AC-8A.2.10")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_put__owner_or_admin_with_step_up__saves_the_next_version(
    world: World, role: Role
) -> None:
    async with world.client(person(role)) as http:
        answer = await http.put(
            path(), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )
        read = await http.get(path())

    assert answer.status_code == 200
    body = answer.json()
    assert body["version"] == 1
    assert body["locale"] == "he-IL"
    assert body["egress_id"] == "shared"
    assert body["shop_languages"] == ["he-IL", "en-US"]
    assert body["baseline"] == {
        "viewport": {"width": 1280, "height": 900},
        "color_scheme": "light",
        "denied_permissions": [
            "camera", "clipboard-read", "clipboard-write", "geolocation", "microphone",
            "notifications",
        ],
    }  # fmt: skip
    assert answer.headers["Cache-Control"] == "no-store"
    assert read.json() == body
    ((_, audit),) = world.store.writes
    assert audit.correlation_id == answer.headers["X-Request-ID"]


async def test_get__before_the_first_save__version_zero(world: World) -> None:
    async with world.client(person(stepped_up=False)) as http:
        answer = await http.get(path())

    assert answer.status_code == 200
    assert answer.json()["version"] == 0
    assert answer.json()["locale"] is None


@pytest.mark.ac("AC-8A.2.10")
async def test_put__refused__each_issue_named_without_its_value_and_nothing_saved(
    world: World,
) -> None:
    secret = "http://user:hunter2@proxy.example:8080"  # noqa: S105 - a fake, never echoed
    async with world.client(person()) as http:
        answer = await http.put(
            path(),
            json={
                "expected_version": 0,
                "settings": {"locale": "fr-FR", "proxy": secret, "user_agent": "Stealth/1"},
            },
        )

    assert answer.status_code == 422
    body = answer.json()
    assert body["type"] == PROBLEMS + "browser-configuration-invalid"
    assert body["issues"] == [
        {"code": "locale_not_recorded", "field": "locale"},
        {"code": "never_configurable", "field": "proxy"},
        {"code": "never_configurable", "field": "user_agent"},
    ]
    assert "hunter2" not in answer.text
    assert "Stealth" not in answer.text
    assert world.store.writes == []


@pytest.mark.ac("AC-8A.2.10")
async def test_put__an_admin_changing_the_egress__refused(world: World) -> None:
    async with world.client(person(Role.ADMIN)) as http:
        answer = await http.put(
            path(),
            json={
                "expected_version": 0,
                "settings": {"locale": "he-IL", "egress_id": "dedicated-1"},
            },
        )

    assert answer.status_code == 422
    assert {"code": "egress_owner_only", "field": "egress_id"} in answer.json()["issues"]


async def test_put__a_stale_version__409_with_the_current_one(world: World) -> None:
    async with world.client(person()) as http:
        await http.put(path(), json={"expected_version": 0, "settings": {"locale": "he-IL"}})
        answer = await http.put(
            path(), json={"expected_version": 0, "settings": {"locale": "en-US"}}
        )

    assert answer.status_code == 409
    assert answer.json()["type"] == PROBLEMS + "browser-configuration-stale"
    assert answer.json()["current_version"] == 1


@pytest.mark.ac("AC-8A.2.10")
@pytest.mark.parametrize(
    ("auth", "problem"),
    [
        (person(stepped_up=False), "step-up-required"),
        (person(Role.ADMIN, stepped_up=False), "step-up-required"),
        (person(assurance=AssuranceLevel.AAL1), "mfa-required"),
        (person(Role.OPERATIONS), "forbidden"),
        (person(Role.VIEWER), "forbidden"),
    ],
)
async def test_put__without_role_mfa_or_step_up__403(
    world: World, auth: AuthContext, problem: str
) -> None:
    async with world.client(auth) as http:
        answer = await http.put(
            path(), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )

    assert answer.status_code == 403
    assert answer.json()["type"] == PROBLEMS + problem
    assert world.store.writes == []


async def test_put__a_bearer_token_alone__never_passes_the_step_up(world: World) -> None:
    """Through the real authenticator: an `aal1` bearer token has neither MFA nor a step-up."""
    async with world.client() as http:
        answer = await http.put(
            path(), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )

    assert answer.status_code == 403
    assert answer.json()["type"] == PROBLEMS + "mfa-required"


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT])
async def test_get__other_roles__403(world: World, role: Role) -> None:
    async with world.client(person(role)) as http:
        answer = await http.get(path())

    assert answer.status_code == 403


async def test_put__another_organizations_shop__404(world: World) -> None:
    async with world.client(person()) as http:
        answer = await http.put(
            path(FOREIGN), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )

    assert answer.status_code == 404
    assert answer.json()["type"] == PROBLEMS + "not-found"


async def test_put__a_shop_without_an_environment__404_not_configured(world: World) -> None:
    async with world.client(person()) as http:
        answer = await http.put(
            path(BARE_SHOP), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )

    assert answer.status_code == 404
    assert answer.json()["type"] == PROBLEMS + "browser-environment-not-configured"


async def test_put__languages_unreadable__503_and_nothing_saved(world: World) -> None:
    world.languages.fail = True
    async with world.client(person()) as http:
        answer = await http.put(
            path(), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )

    assert answer.status_code == 503
    assert answer.json()["type"] == PROBLEMS + "shop-languages-unavailable"
    assert world.store.writes == []


async def test_put__not_wired__503(world: World) -> None:
    async with world.client(person(), configured=False) as http:
        answer = await http.put(
            path(), json={"expected_version": 0, "settings": {"locale": "he-IL"}}
        )

    assert answer.status_code == 503
    assert answer.json()["type"] == PROBLEMS + "browser-configuration-unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"settings": {"locale": "he-IL"}},
        {"expected_version": -1, "settings": {"locale": "he-IL"}},
        {"expected_version": 0, "settings": {"locale": "he-IL"}, "extra": 1},
        {"expected_version": 0, "settings": {f"k{i}": i for i in range(21)}},
        {"expected_version": 0, "settings": ["locale"]},
    ],
)
async def test_put__a_malformed_body__422_validation(world: World, body: dict[str, Any]) -> None:
    async with world.client(person()) as http:
        answer = await http.put(path(), json=body)

    assert answer.status_code == 422
    assert answer.json()["type"] == PROBLEMS + "validation-error"
    assert world.store.writes == []
