"""Saving and reading a shop's browser configuration profile (slice 8A.2b, AC-8A.2.10).

The Owner or an Admin saves a new version with MFA and a recent step-up; it is validated against
the shop's recorded languages and egress allocations, and audited in the same write. Only the
Owner changes the egress. Nothing is saved when any issue is found. Agents and automations have
no tool for any of this."""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.access.policy import Capability, Outcome
from htco_application.browser_configuration import (
    BrowserConfigurationForbiddenError,
    ConfigurationServices,
    ConfiguredEnvironment,
    MemoryConfigurationStore,
    SharedEgressOnly,
    ShopLanguagesUnavailableError,
    StaleConfigurationError,
    browser_configuration,
    save_browser_configuration,
)
from htco_application.browser_environments import EnvironmentNotFoundError
from htco_application.shops import ShopNotFoundError
from htco_application.workforce.files import read_registry
from htco_domain.actors import ActorKind
from htco_domain.browser import EnvironmentStatus
from htco_domain.browser_configuration import ConfigurationRefusedError, ConfigurationVersion
from htco_domain.identity import AssuranceLevel, Role
from tests.execution_fakes import REGISTRY, real_catalog

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("c3a00000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("c3b00000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("c3a10000-0000-0000-0000-000000000001")
BARE_SHOP = uuid.UUID("c3a10000-0000-0000-0000-000000000002")
FOREIGN_SHOP = uuid.UUID("c3b10000-0000-0000-0000-000000000001")
ENV = uuid.UUID("c3a20000-0000-0000-0000-000000000001")
USER = uuid.UUID("c3a00000-0000-0000-0000-0000000000a1")


class Languages:
    def __init__(self, languages: tuple[str, ...] = ("he-IL", "en-US")) -> None:
        self.languages = languages
        self.calls = 0
        self.fail = False

    async def recorded(self, auth: AuthContext, shop_id: uuid.UUID) -> tuple[str, ...]:
        assert auth.user_id == USER
        assert shop_id == SHOP
        self.calls += 1
        if self.fail:
            raise ShopLanguagesUnavailableError
        return self.languages


class Allocated:
    def __init__(self, *egress: str) -> None:
        self.egress = frozenset(egress)

    async def allocated(self, shop_id: uuid.UUID) -> frozenset[str]:  # noqa: ARG002
        return self.egress


def world(
    *, languages: Languages | None = None, egress: Allocated | None = None
) -> tuple[ConfigurationServices, MemoryConfigurationStore]:
    store = MemoryConfigurationStore(
        shops={SHOP: ORG, BARE_SHOP: ORG, FOREIGN_SHOP: OTHER_ORG},
        environments={
            SHOP: ConfiguredEnvironment(ENV, ORG, SHOP, EnvironmentStatus.READY),
            FOREIGN_SHOP: ConfiguredEnvironment(
                uuid.uuid4(), OTHER_ORG, FOREIGN_SHOP, EnvironmentStatus.READY
            ),
        },
    )
    services = ConfigurationServices(
        store=store,
        languages=languages or Languages(),
        egress=egress or SharedEgressOnly(),
    )
    return services, store


def person(
    role: Role = Role.OWNER,
    *,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    stepped_up: bool = True,
    org: uuid.UUID = ORG,
) -> AuthContext:
    return AuthContext(
        user_id=USER,
        assurance=assurance,
        session_id="s-1",
        memberships=(Membership(org, role, frozenset({SHOP})),),
        reauthenticated_at=NOW - timedelta(minutes=2) if stepped_up else None,
    )


async def save(  # noqa: PLR0913 - the edit and its context
    services: ConfigurationServices,
    settings: dict[str, object],
    *,
    auth: AuthContext | None = None,
    expected: int = 0,
    shop: uuid.UUID = SHOP,
    correlation: str = "req-1",
) -> ConfigurationVersion:
    return await save_browser_configuration(
        auth or person(), services, shop, settings=settings, expected_version=expected,
        correlation_id=correlation, now=NOW,
    )  # fmt: skip


# --- Saving -----------------------------------------------------------------------------------


@pytest.mark.ac("AC-8A.2.10")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_save__owner_or_admin_with_step_up__a_new_version_and_its_audit(role: Role) -> None:
    services, store = world()

    saved = await save(services, {"locale": "he-IL"}, auth=person(role))

    assert saved == store.versions[ENV][-1]
    assert saved.version == 1
    assert saved.settings.locale == "he-IL"
    assert saved.settings.egress_id == "shared"
    assert saved.shop_languages == ("he-IL", "en-US")
    assert saved.created_by.kind is ActorKind.USER
    assert saved.created_by.id == str(USER)
    ((version, audit),) = store.writes
    assert version is saved
    assert audit.action == "browser_configuration.saved"
    assert audit.entity_type == "browser_environment"
    assert audit.entity_id == str(ENV)
    assert audit.organization_id == ORG
    assert audit.before is None
    assert audit.after == {"version": 1, "locale": "he-IL", "egress_id": "shared"}
    assert audit.correlation_id == "req-1"


@pytest.mark.ac("AC-8A.2.10")
async def test_save__each_accepted_change_is_a_new_version() -> None:
    services, store = world()
    await save(services, {"locale": "he-IL"})

    second = await save(services, {"locale": "en-US"}, expected=1, correlation="req-2")

    assert [v.version for v in store.versions[ENV]] == [1, 2]
    assert second.settings.locale == "en-US"
    assert store.writes[-1][1].before == {"version": 1, "locale": "he-IL", "egress_id": "shared"}


async def test_save__the_same_settings_again__no_new_version_and_no_audit() -> None:
    services, store = world()
    first = await save(services, {"locale": "he-IL"})

    again = await save(services, {"locale": "he-il", "egress_id": "shared"}, expected=1)

    assert again == first
    assert len(store.writes) == 1


async def test_save__the_recorded_languages_are_kept_canonical_and_once_each() -> None:
    services, _ = world(languages=Languages(("he-il", "en-US", "he-IL", "not a tag", "")))

    saved = await save(services, {"locale": "he-IL"})

    assert saved.shop_languages == ("he-IL", "en-US")


@pytest.mark.ac("AC-8A.2.10")
async def test_save__any_issue__refused_by_name_and_nothing_saved() -> None:
    services, store = world()

    with pytest.raises(ConfigurationRefusedError) as refused:
        await save(services, {"locale": "fr-FR", "user_agent": "Mozilla/5.0 (Stealth)"})

    assert {(i.code, i.field) for i in refused.value.issues} == {
        ("locale_not_recorded", "locale"),
        ("never_configurable", "user_agent"),
    }
    assert store.writes == []
    assert ENV not in store.versions


@pytest.mark.ac("AC-8A.2.10")
async def test_save__an_unallocated_dedicated_egress__refused() -> None:
    services, store = world()

    with pytest.raises(ConfigurationRefusedError) as refused:
        await save(services, {"locale": "he-IL", "egress_id": "dedicated-1"})

    assert {(i.code, i.field) for i in refused.value.issues} == {
        ("egress_not_allocated", "egress_id")
    }
    assert store.writes == []


async def test_save__the_owner_moves_the_shop_to_its_allocated_dedicated_egress() -> None:
    services, _ = world(egress=Allocated("dedicated-1"))

    saved = await save(services, {"locale": "he-IL", "egress_id": "dedicated-1"})

    assert saved.settings.dedicated_egress


@pytest.mark.ac("AC-8A.2.10")
async def test_save__an_admin_cannot_change_the_egress() -> None:
    services, store = world(egress=Allocated("dedicated-1"))

    with pytest.raises(ConfigurationRefusedError) as refused:
        await save(
            services, {"locale": "he-IL", "egress_id": "dedicated-1"}, auth=person(Role.ADMIN)
        )

    assert {(i.code, i.field) for i in refused.value.issues} == {("egress_owner_only", "egress_id")}
    assert store.writes == []


async def test_save__an_admin_keeps_the_egress_the_owner_chose() -> None:
    services, _ = world(egress=Allocated("dedicated-1"))
    await save(services, {"locale": "he-IL", "egress_id": "dedicated-1"})

    saved = await save(
        services, {"locale": "en-US", "egress_id": "dedicated-1"}, auth=person(Role.ADMIN),
        expected=1,
    )  # fmt: skip

    assert saved.settings.egress_id == "dedicated-1"


async def test_save__an_admin_who_leaves_the_egress_out_keeps_the_current_one() -> None:
    services, _ = world(egress=Allocated("dedicated-1"))
    await save(services, {"locale": "he-IL", "egress_id": "dedicated-1"})

    saved = await save(services, {"locale": "en-US"}, auth=person(Role.ADMIN), expected=1)

    assert saved.settings.egress_id == "dedicated-1"


@pytest.mark.ac("AC-8A.2.10")
@pytest.mark.parametrize(
    ("auth", "outcome"),
    [
        (person(Role.OWNER, stepped_up=False), Outcome.STEP_UP_REQUIRED),
        (person(Role.ADMIN, stepped_up=False), Outcome.STEP_UP_REQUIRED),
        (person(Role.OWNER, assurance=AssuranceLevel.AAL1), Outcome.MFA_REQUIRED),
        (person(Role.OPERATIONS), Outcome.DENY),
        (person(Role.DEVELOPER), Outcome.DENY),
        (person(Role.VIEWER), Outcome.DENY),
    ],
)
async def test_save__without_the_role_mfa_or_step_up__refused_before_anything_is_read(
    auth: AuthContext, outcome: Outcome
) -> None:
    languages = Languages()
    services, store = world(languages=languages)

    with pytest.raises(BrowserConfigurationForbiddenError) as refused:
        await save(services, {"locale": "he-IL"}, auth=auth)

    assert refused.value.outcome is outcome
    assert languages.calls == 0
    assert store.writes == []


async def test_save__another_organizations_shop__does_not_exist() -> None:
    services, store = world()

    with pytest.raises(ShopNotFoundError):
        await save(services, {"locale": "he-IL"}, shop=FOREIGN_SHOP)
    with pytest.raises(ShopNotFoundError):
        await save(services, {"locale": "he-IL"}, shop=uuid.uuid4())
    assert store.writes == []


async def test_save__a_shop_without_an_environment__environment_not_found() -> None:
    services, _ = world()

    with pytest.raises(EnvironmentNotFoundError):
        await save(services, {"locale": "he-IL"}, shop=BARE_SHOP)


async def test_save__a_deleted_environment__environment_not_found() -> None:
    services, store = world()
    store.environments[SHOP] = ConfiguredEnvironment(
        ENV, ORG, SHOP, EnvironmentStatus.NOT_CONFIGURED
    )

    with pytest.raises(EnvironmentNotFoundError):
        await save(services, {"locale": "he-IL"})


async def test_save__a_stale_expected_version__refused_with_the_current_one() -> None:
    services, store = world()
    await save(services, {"locale": "he-IL"})

    with pytest.raises(StaleConfigurationError) as stale:
        await save(services, {"locale": "en-US"}, expected=0)

    assert stale.value.current_version == 1
    assert len(store.writes) == 1


async def test_save__two_writers_of_the_same_version__only_one_gets_in() -> None:
    services, store = world()

    results = await asyncio.gather(
        save(services, {"locale": "he-IL"}, correlation="a"),
        save(services, {"locale": "en-US"}, correlation="b"),
        return_exceptions=True,
    )

    assert sorted(type(r).__name__ for r in results) == [
        "ConfigurationVersion", "StaleConfigurationError",
    ]  # fmt: skip
    assert len(store.writes) == 1


async def test_save__the_languages_cannot_be_read__nothing_saved() -> None:
    languages = Languages()
    languages.fail = True
    services, store = world(languages=languages)

    with pytest.raises(ShopLanguagesUnavailableError):
        await save(services, {"locale": "he-IL"})
    assert store.writes == []


# --- Reading ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_read__owner_and_admin__the_current_version(role: Role) -> None:
    services, _ = world()
    saved = await save(services, {"locale": "he-IL"})

    found = await browser_configuration(person(role, stepped_up=False), services, SHOP, now=NOW)

    assert found == saved


async def test_read__no_version_yet__none() -> None:
    services, _ = world()

    assert await browser_configuration(person(), services, SHOP, now=NOW) is None


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.VIEWER, Role.FINANCE])
async def test_read__other_roles__refused(role: Role) -> None:
    services, _ = world()

    with pytest.raises(BrowserConfigurationForbiddenError):
        await browser_configuration(person(role), services, SHOP, now=NOW)


async def test_read__another_organizations_shop__does_not_exist() -> None:
    services, _ = world()

    with pytest.raises(ShopNotFoundError):
        await browser_configuration(person(), services, FOREIGN_SHOP, now=NOW)


# --- No tool for agents or automations (AC-8A.2.10) ------------------------------------------


@pytest.mark.ac("AC-8A.2.10")
def test_registry__no_tool_or_action_changes_a_browser_configuration() -> None:
    registry = read_registry(REGISTRY).registry()
    catalog = real_catalog(registry)
    names = [*registry.tools, *catalog.actions]
    pattern = re.compile(r"browser[._-]?(config|configuration|settings|profile|egress)")

    assert names
    assert [name for name in names if pattern.search(name)] == []


@pytest.mark.ac("AC-8A.2.10")
def test_capability__granted_to_the_owner_and_admins_only_with_mfa_and_step_up() -> None:
    from htco_application.access.policy import PERMISSIONS  # noqa: PLC0415

    grants = PERMISSIONS[Capability.EDIT_BROWSER_CONFIGURATION]
    assert set(grants) == {Role.OWNER, Role.ADMIN}
    assert all(grant.mfa and grant.step_up for grant in grants.values())
