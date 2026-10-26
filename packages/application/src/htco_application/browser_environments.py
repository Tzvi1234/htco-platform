"""The Shop Environment Registry (slice 5.16, ADR-0020, `execution-engine.md` §3 and §7).

Contracts only: no browser runs in Phase 5. The registry keeps each shop's optional browser
environment and moves it through the transition table of `htco_domain.browser`; every change is
stored with its actor, reason and correlation ID in the append-only history, and published as
`browser.environment_status_changed.v1` through the outbox, in the same transaction.

- `ShopEnvironmentRegistry` is the port; `EnvironmentRegistry` implements it on an
  `EnvironmentStore` (Postgres in `htco_persistence.browser_environments`, in memory here).
- Creating an environment is off until Phase 8A (the `browser_environments` flag): adding a shop
  stays OAuth only, and a shop without an environment is `NOT_CONFIGURED` (AC-5.16.4).
- `EnvironmentProvisioner` is the port of the Phase 8A provisioner; `FakeEnvironmentProvisioner`
  answers from a script and never starts a browser.
- `EnvironmentBrowserGate` is the router's `BrowserGate` (slice 5.13) over the registry: a
  browser binding needs `READY`. Every Etsy browser flag is off until Phase 8A records its scope
  (ADR-0022), so the flag and scope answers stay empty.
- `browser_environment_status` is the read use case behind
  `GET /v1/shops/{shop_id}/browser-environment`: the status only, for the Owner and Admins.
"""

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, ShopRef, decide
from htco_application.jobs import JsonValue
from htco_application.messaging import OutboxMessage
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.actors import ActorRef
from htco_domain.browser import (
    BrowserEnvironment,
    ConsentRecord,
    EnvironmentChange,
    EnvironmentStatus,
    HealthReport,
)

STATUS_CHANGED_EVENT = "browser.environment_status_changed.v1"
EVENT_DESTINATION = "events.publish"
CREATION_FLAG = "browser_environments"


class EnvironmentCreationDisabledError(Exception):
    """Creating a browser environment is off on this server (until Phase 8A)."""


class EnvironmentExistsError(Exception):
    pass


class EnvironmentNotFoundError(Exception):
    pass


class StaleEnvironmentError(Exception):
    """The environment is not in the status the caller expected: someone changed it meanwhile."""

    def __init__(self, expected: EnvironmentStatus, actual: EnvironmentStatus) -> None:
        super().__init__(f"expected {expected.value}, found {actual.value}")
        self.expected = expected
        self.actual = actual


# --- Ports --------------------------------------------------------------------------------------


class EnvironmentStore(Protocol):
    def by_shop(self, shop_id: uuid.UUID) -> BrowserEnvironment | None: ...

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None: ...

    def insert(
        self, environment: BrowserEnvironment, change: EnvironmentChange, event: OutboxMessage
    ) -> bool:
        """In one transaction: the row, its first change and its event. False when the shop
        has an environment already."""
        ...

    def apply(
        self, after: BrowserEnvironment, change: EnvironmentChange, event: OutboxMessage
    ) -> bool:
        """In one transaction, only while the row is still in `change.from_status`: the new
        status, the change and the event. False when the status moved meanwhile."""
        ...

    def add_health(
        self,
        environment: BrowserEnvironment,
        report: HealthReport,
        *,
        by: ActorRef,
        correlation_id: str,
    ) -> None: ...


class ShopOwners(Protocol):
    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None: ...


class ShopEnvironmentRegistry(Protocol):
    def get(self, shop_id: uuid.UUID) -> BrowserEnvironment | None: ...

    def create(
        self, shop_id: uuid.UUID, *, by: ActorRef, consent: ConsentRecord, correlation_id: str
    ) -> BrowserEnvironment: ...

    def transition(  # noqa: PLR0913 - the target and its recorded context
        self,
        environment_id: uuid.UUID,
        target: EnvironmentStatus,
        *,
        reason: str,
        by: ActorRef,
        expected: EnvironmentStatus,
        correlation_id: str,
    ) -> BrowserEnvironment: ...

    def record_health(
        self,
        environment_id: uuid.UUID,
        report: HealthReport,
        *,
        by: ActorRef,
        correlation_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class ProvisioningResult:
    """Where provisioning left the environment: READY, LOGIN_REQUIRED or FAILED."""

    status: EnvironmentStatus
    reason: str

    def __post_init__(self) -> None:
        if self.status not in _PROVISIONED:
            raise ValueError("provisioning ends READY, LOGIN_REQUIRED or FAILED")


_PROVISIONED = frozenset(
    {EnvironmentStatus.READY, EnvironmentStatus.LOGIN_REQUIRED, EnvironmentStatus.FAILED}
)


class EnvironmentProvisioner(Protocol):  # contract in Phase 5, real in 8A
    async def provision(self, environment_id: uuid.UUID) -> ProvisioningResult: ...

    async def deprovision(self, environment_id: uuid.UUID) -> None: ...


# --- The registry -------------------------------------------------------------------------------


def status_event(change: EnvironmentChange) -> OutboxMessage:
    """IDs and statuses only (EVENT_CATALOG rule 3): never the reason text or the consent."""
    payload: dict[str, JsonValue] = {
        "event_type": STATUS_CHANGED_EVENT,
        "environment_id": str(change.environment_id),
        "shop_id": str(change.shop_id),
        "from_status": change.from_status.value,
        "to_status": change.to_status.value,
        "actor": str(change.actor),
        "occurred_at": change.occurred_at.isoformat(),
    }
    return OutboxMessage(
        organization_id=change.organization_id,
        shop_id=change.shop_id,
        destination=EVENT_DESTINATION,
        # One event per recorded change: a change is one (environment, target, time).
        idempotency_key=(
            f"{STATUS_CHANGED_EVENT}:{change.environment_id}:{change.to_status.value}:"
            f"{change.occurred_at.isoformat()}"
        ),
        payload=payload,
        correlation_id=change.correlation_id,
    )


class EnvironmentRegistry:
    def __init__(
        self,
        store: EnvironmentStore,
        shops: ShopOwners,
        clock: Callable[[], datetime],
        *,
        creation_enabled: bool = False,
        new_id: Callable[[], uuid.UUID] = uuid.uuid7,
    ) -> None:
        self._store = store
        self._shops = shops
        self._clock = clock
        self._creation_enabled = creation_enabled
        self._new_id = new_id

    def get(self, shop_id: uuid.UUID) -> BrowserEnvironment | None:
        return self._store.by_shop(shop_id)

    def status_of(self, shop_id: uuid.UUID) -> EnvironmentStatus:
        environment = self._store.by_shop(shop_id)
        return EnvironmentStatus.NOT_CONFIGURED if environment is None else environment.status

    def create(
        self, shop_id: uuid.UUID, *, by: ActorRef, consent: ConsentRecord, correlation_id: str
    ) -> BrowserEnvironment:
        """NOT_CONFIGURED → PROVISIONING. The caller (Phase 8A's route) has checked the role,
        step-up and the consent shown; the registry refuses while the flag is off."""
        if not self._creation_enabled:
            raise EnvironmentCreationDisabledError(f"the {CREATION_FLAG} flag is off")
        organization = self._shops.organization_of_shop(shop_id)
        if organization is None:
            raise ShopNotFoundError("no such shop")
        existing = self._store.by_shop(shop_id)
        if existing is not None:
            if existing.status is not EnvironmentStatus.NOT_CONFIGURED:
                raise EnvironmentExistsError(f"the shop's environment is {existing.status.value}")
            # Deleted before: the same row is provisioned again, with its history kept.
            again = replace(existing, consent=consent)
            return self._move(again, EnvironmentStatus.PROVISIONING, by=by, reason="created",
                              correlation_id=correlation_id)  # fmt: skip
        created = BrowserEnvironment(
            id=self._new_id(), organization_id=organization, shop_id=shop_id,
            status=EnvironmentStatus.PROVISIONING, consent=consent,
        )  # fmt: skip
        change = EnvironmentChange.creation(
            created, by=by, reason="created", correlation_id=correlation_id, at=self._clock()
        )
        if not self._store.insert(created, change, status_event(change)):
            raise EnvironmentExistsError("the shop has an environment already")
        return created

    def transition(  # noqa: PLR0913 - the target and its recorded context
        self,
        environment_id: uuid.UUID,
        target: EnvironmentStatus,
        *,
        reason: str,
        by: ActorRef,
        expected: EnvironmentStatus,
        correlation_id: str,
    ) -> BrowserEnvironment:
        """Raises `EnvironmentNotFoundError`, `StaleEnvironmentError` (not in `expected`) or
        `IllegalTransition` (an edge outside the table)."""
        current = self._load(environment_id)
        if current.status is not expected:
            raise StaleEnvironmentError(expected, current.status)
        return self._move(current, target, by=by, reason=reason, correlation_id=correlation_id)

    def record_health(
        self,
        environment_id: uuid.UUID,
        report: HealthReport,
        *,
        by: ActorRef,
        correlation_id: str,
    ) -> None:
        """Stores the report in the history; deciding a status from it is Phase 8A's job."""
        self._store.add_health(
            self._load(environment_id), report, by=by, correlation_id=correlation_id
        )

    def _load(self, environment_id: uuid.UUID) -> BrowserEnvironment:
        environment = self._store.by_id(environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(str(environment_id))
        return environment

    def _move(
        self,
        current: BrowserEnvironment,
        target: EnvironmentStatus,
        *,
        by: ActorRef,
        reason: str,
        correlation_id: str,
    ) -> BrowserEnvironment:
        change = EnvironmentChange.of(
            current, target, by=by, reason=reason, correlation_id=correlation_id,
            at=self._clock(),
        )  # fmt: skip
        after = current.transition(target)
        if not self._store.apply(after, change, status_event(change)):
            actual = self._load(current.id).status
            raise StaleEnvironmentError(current.status, actual)
        return after


class EnvironmentBrowserGate:
    """The router's view of browser environments (slice 5.13's `BrowserGate`)."""

    def __init__(self, registry: EnvironmentRegistry) -> None:
        self._registry = registry

    def environment_status(self, shop_id: uuid.UUID) -> EnvironmentStatus | None:
        return self._registry.status_of(shop_id)

    def flag_enabled_at(self, action_name: str) -> datetime | None:  # noqa: ARG002
        return None  # every Etsy browser flag is off until Phase 8A (ADR-0022)

    def scope_recorded_at(self, shop_id: uuid.UUID, scope_id: str) -> datetime | None:  # noqa: ARG002
        return None  # no scope row is recorded yet (etsy-automation-approval.md)


# --- In memory ----------------------------------------------------------------------------------


@dataclass
class MemoryEnvironmentStore:
    """The Postgres store's rules in memory: one environment per shop, compare-and-set status,
    an append-only history and the outbox events."""

    environments: dict[uuid.UUID, BrowserEnvironment] = field(default_factory=dict)
    changes: list[EnvironmentChange] = field(default_factory=list)
    health: list[tuple[uuid.UUID, HealthReport, ActorRef, str]] = field(default_factory=list)
    events: list[OutboxMessage] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def by_shop(self, shop_id: uuid.UUID) -> BrowserEnvironment | None:
        with self._lock:
            return next((e for e in self.environments.values() if e.shop_id == shop_id), None)

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None:
        with self._lock:
            return self.environments.get(environment_id)

    def insert(
        self, environment: BrowserEnvironment, change: EnvironmentChange, event: OutboxMessage
    ) -> bool:
        with self._lock:
            if any(e.shop_id == environment.shop_id for e in self.environments.values()):
                return False
            self.environments[environment.id] = environment
            self.changes.append(change)
            self.events.append(event)
            return True

    def apply(
        self, after: BrowserEnvironment, change: EnvironmentChange, event: OutboxMessage
    ) -> bool:
        with self._lock:
            current = self.environments.get(after.id)
            if current is None or current.status is not change.from_status:
                return False
            self.environments[after.id] = after
            self.changes.append(change)
            self.events.append(event)
            return True

    def recent_health(self, environment_id: uuid.UUID, limit: int) -> tuple[bool, ...]:
        with self._lock:
            mine = [report for (env, report, _, _) in self.health if env == environment_id]
            return tuple(report.ok for report in reversed(mine))[:limit]

    def add_health(
        self,
        environment: BrowserEnvironment,
        report: HealthReport,
        *,
        by: ActorRef,
        correlation_id: str,
    ) -> None:
        with self._lock:
            self.health.append((environment.id, report, by, correlation_id))
            current = self.environments[environment.id]
            self.environments[environment.id] = replace(
                current, last_health_at=report.checked_at, last_health_ok=report.ok
            )


@dataclass
class FakeEnvironmentProvisioner:
    """Answers from a script; never starts a browser (the real provisioner is Phase 8A)."""

    results: Mapping[uuid.UUID, ProvisioningResult] = field(default_factory=dict)
    default: ProvisioningResult = field(
        default_factory=lambda: ProvisioningResult(EnvironmentStatus.READY, "preflight_passed")
    )
    provisioned: list[uuid.UUID] = field(default_factory=list)
    deprovisioned: list[uuid.UUID] = field(default_factory=list)

    async def provision(self, environment_id: uuid.UUID) -> ProvisioningResult:
        self.provisioned.append(environment_id)
        return self.results.get(environment_id, self.default)

    async def deprovision(self, environment_id: uuid.UUID) -> None:
        self.deprovisioned.append(environment_id)


# --- Reading, for the office --------------------------------------------------------------------


class EnvironmentReader(Protocol):
    def status_for_user(
        self, claims: Mapping[str, Any], shop_id: uuid.UUID
    ) -> EnvironmentStatus | None:
        """The shop's environment status as the signed-in user sees it (RLS), None without a
        row."""
        ...


def browser_environment_status(
    auth: AuthContext,
    reader: EnvironmentReader,
    shops: ShopOwners,
    shop_id: uuid.UUID,
    *,
    now: datetime,
) -> EnvironmentStatus:
    """The status only. A shop the caller cannot see does not exist (`ShopNotFoundError`); a role
    that may see the shop but not its environment is refused (`ShopAccessDeniedError`)."""
    organization = shops.organization_of_shop(shop_id)
    if organization is None:
        raise ShopNotFoundError("no such shop")
    try:
        principal = auth.principal_for(organization)
    except NoMembershipError:
        raise ShopNotFoundError("no such shop") from None
    shop = ShopRef(id=shop_id, organization_id=organization)
    if decide(principal, Capability.VIEW_OVERVIEW, shop=shop, now=now).outcome is not Outcome.ALLOW:
        raise ShopNotFoundError("no such shop")
    decision = decide(principal, Capability.VIEW_BROWSER_ENVIRONMENT, shop=shop, now=now)
    if decision.outcome is not Outcome.ALLOW:
        raise ShopAccessDeniedError("only the Owner and Admins see the browser environment")
    status = reader.status_for_user(auth.claims, shop_id)
    return EnvironmentStatus.NOT_CONFIGURED if status is None else status
