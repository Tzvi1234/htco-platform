"""A shop's browser configuration profile: reading it and saving a new version (slice 8A.2b,
ADR-0025 §2, AC-8A.2.10).

- `save_browser_configuration` is the only way a version is written. The Owner or an Admin of
  the shop's organization saves it with MFA and a recent step-up
  (`Capability.EDIT_BROWSER_CONFIGURATION`). The settings are validated against the shop's
  recorded languages and its egress allocations (`htco_domain.browser_configuration`); any
  issue refuses the save with every issue named, and nothing is written. Changing the egress
  stays Owner-only. An accepted change is the next version, written in one transaction with its
  audit record; the same settings again write nothing.
- `browser_configuration` reads the current version for the Owner and Admins.

The actor is always the signed-in person: no agent, automation or tool reaches these functions,
and the database refuses a version written by anything but a user.

The ports are async (AUD-05): each adapter owns its thread hop.
"""

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import (
    Capability,
    Outcome,
    Principal,
    ShopRef,
    decide,
)
from htco_application.audit import AuditRecord
from htco_application.browser_environments import EnvironmentNotFoundError
from htco_application.shops import ShopNotFoundError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import EnvironmentStatus
from htco_domain.browser_configuration import (
    EGRESS,
    MAX_LANGUAGES,
    SHARED_EGRESS,
    ConfigurationIssue,
    ConfigurationRefusedError,
    ConfigurationVersion,
    canonical_locale,
    validate_settings,
)
from htco_domain.identity import Role

SAVED_ACTION = "browser_configuration.saved"
ENTITY_TYPE = "browser_environment"


class BrowserConfigurationForbiddenError(Exception):
    """The caller may see the shop but not do this: the role, MFA or the step-up is missing."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class StaleConfigurationError(Exception):
    """The caller edited another version than the current one: someone saved meanwhile."""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"the current version is {current_version}")
        self.current_version = current_version


class ShopLanguagesUnavailableError(Exception):
    """The shop's recorded languages could not be read now; nothing was saved."""


@dataclass(frozen=True)
class ConfiguredEnvironment:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    status: EnvironmentStatus


# --- Ports --------------------------------------------------------------------------------------


class ConfigurationStore(Protocol):
    async def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None: ...

    async def environment_of_shop(self, shop_id: uuid.UUID) -> ConfiguredEnvironment | None: ...

    async def current(self, environment_id: uuid.UUID) -> ConfigurationVersion | None: ...

    async def append(self, version: ConfigurationVersion, audit: AuditRecord) -> bool:
        """In one transaction: the version and its audit record, only while `version.version`
        is the next one. False when someone saved that version first."""
        ...


class ShopLanguages(Protocol):
    async def recorded(self, auth: AuthContext, shop_id: uuid.UUID) -> tuple[str, ...]:
        """The languages the shop is operated in, as its marketplace records them. Raises
        `ShopLanguagesUnavailableError` when they cannot be read now."""
        ...


class EgressAllocations(Protocol):
    async def allocated(self, shop_id: uuid.UUID) -> frozenset[str]:
        """The dedicated egress IDs the Owner allocated to this shop (8A.3's `NetworkProvider`)."""
        ...


class SharedEgressOnly:
    """No shop has a dedicated egress yet (D-18 (c)): every shop uses the shared one."""

    async def allocated(self, shop_id: uuid.UUID) -> frozenset[str]:  # noqa: ARG002
        return frozenset()


@dataclass(frozen=True)
class ConfigurationServices:
    store: ConfigurationStore
    languages: ShopLanguages
    egress: EgressAllocations = field(default_factory=SharedEgressOnly)


# --- Use cases ----------------------------------------------------------------------------------


async def _principal(
    auth: AuthContext, store: ConfigurationStore, shop_id: uuid.UUID, now: datetime
) -> tuple[Principal, ShopRef]:
    """A shop the caller cannot see does not exist for them."""
    organization = await store.organization_of_shop(shop_id)
    if organization is None:
        raise ShopNotFoundError("no such shop")
    try:
        principal = auth.principal_for(organization)
    except NoMembershipError:
        raise ShopNotFoundError("no such shop") from None
    shop = ShopRef(id=shop_id, organization_id=organization)
    if decide(principal, Capability.VIEW_OVERVIEW, shop=shop, now=now).outcome is not Outcome.ALLOW:
        raise ShopNotFoundError("no such shop")
    return principal, shop


def _require(principal: Principal, capability: Capability, shop: ShopRef, now: datetime) -> None:
    outcome = decide(principal, capability, shop=shop, now=now).outcome
    if outcome is not Outcome.ALLOW:
        raise BrowserConfigurationForbiddenError(outcome)


async def _environment(store: ConfigurationStore, shop_id: uuid.UUID) -> ConfiguredEnvironment:
    environment = await store.environment_of_shop(shop_id)
    if environment is None or environment.status is EnvironmentStatus.NOT_CONFIGURED:
        raise EnvironmentNotFoundError("the shop has no browser environment")
    return environment


async def browser_configuration(
    auth: AuthContext, services: ConfigurationServices, shop_id: uuid.UUID, *, now: datetime
) -> ConfigurationVersion | None:
    """The current version, or None before the first save."""
    principal, shop = await _principal(auth, services.store, shop_id, now)
    _require(principal, Capability.VIEW_BROWSER_ENVIRONMENT, shop, now)
    environment = await _environment(services.store, shop_id)
    return await services.store.current(environment.id)


def _snapshot(languages: tuple[str, ...]) -> tuple[str, ...]:
    """Canonical tags, each once, in the marketplace's order; anything else is dropped."""
    found: list[str] = []
    for language in languages:
        tag = canonical_locale(language)
        if tag is not None and tag not in found:
            found.append(tag)
    return tuple(found[:MAX_LANGUAGES])


async def save_browser_configuration(  # noqa: PLR0913 - the caller, the shop and the edit
    auth: AuthContext,
    services: ConfigurationServices,
    shop_id: uuid.UUID,
    *,
    settings: Mapping[str, Any],
    expected_version: int,
    correlation_id: str,
    now: datetime,
) -> ConfigurationVersion:
    """Raises `ShopNotFoundError`, `BrowserConfigurationForbiddenError`,
    `EnvironmentNotFoundError`, `StaleConfigurationError`, `ShopLanguagesUnavailableError` or
    `ConfigurationRefusedError` (every issue named; nothing saved)."""
    store = services.store
    principal, shop = await _principal(auth, store, shop_id, now)
    _require(principal, Capability.EDIT_BROWSER_CONFIGURATION, shop, now)
    environment = await _environment(store, shop_id)
    current = await store.current(environment.id)
    current_version = 0 if current is None else current.version
    if expected_version != current_version:
        raise StaleConfigurationError(current_version)
    current_egress = SHARED_EGRESS if current is None else current.settings.egress_id
    # Leaving the egress out keeps the current one, so an Admin can edit the locale alone.
    raw = dict(settings)
    raw.setdefault(EGRESS, current_egress)
    languages, allocated = await asyncio.gather(
        services.languages.recorded(auth, shop_id), services.egress.allocated(shop_id)
    )
    snapshot = _snapshot(languages)
    issues: list[ConfigurationIssue] = []
    validated = None
    try:
        validated = validate_settings(raw, shop_languages=snapshot, allocated_egress=allocated)
    except ConfigurationRefusedError as refused:
        issues += refused.issues
    if raw[EGRESS] != current_egress and principal.role is not Role.OWNER:
        issues.append(ConfigurationIssue("egress_owner_only", EGRESS))
    if issues or validated is None:
        raise ConfigurationRefusedError(issues)
    if current is not None and current.settings == validated:
        return current  # not a change: no new version, nothing to audit
    actor = ActorRef(ActorKind.USER, str(auth.user_id))
    version = ConfigurationVersion(
        environment_id=environment.id, organization_id=environment.organization_id,
        shop_id=shop_id, version=current_version + 1, settings=validated,
        shop_languages=snapshot, created_by=actor, correlation_id=correlation_id,
        created_at=now,
    )  # fmt: skip
    audit = AuditRecord.new(
        organization_id=environment.organization_id, actor=actor, action=SAVED_ACTION,
        entity_type=ENTITY_TYPE, entity_id=str(environment.id),
        before=None if current is None else current.as_audit(), after=version.as_audit(),
        correlation_id=correlation_id,
    )  # fmt: skip
    if not await store.append(version, audit):
        latest = await store.current(environment.id)
        raise StaleConfigurationError(0 if latest is None else latest.version)
    return version


# --- In memory ----------------------------------------------------------------------------------


@dataclass
class MemoryConfigurationStore:
    """The Postgres store's rules in memory: the next version only, with its audit."""

    shops: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    environments: dict[uuid.UUID, ConfiguredEnvironment] = field(default_factory=dict)
    versions: dict[uuid.UUID, list[ConfigurationVersion]] = field(default_factory=dict)
    writes: list[tuple[ConfigurationVersion, AuditRecord]] = field(default_factory=list)

    async def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return self.shops.get(shop_id)

    async def environment_of_shop(self, shop_id: uuid.UUID) -> ConfiguredEnvironment | None:
        return self.environments.get(shop_id)

    async def current(self, environment_id: uuid.UUID) -> ConfigurationVersion | None:
        found = self.versions.get(environment_id)
        return found[-1] if found else None

    async def append(self, version: ConfigurationVersion, audit: AuditRecord) -> bool:
        found = self.versions.setdefault(version.environment_id, [])
        if version.version != len(found) + 1:
            return False
        found.append(version)
        self.writes.append((version, audit))
        return True
