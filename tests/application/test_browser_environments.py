"""The Shop Environment Registry (slice 5.16): create behind its flag, transitions with their
history and events, health reports, the router's gate and the status read for the office."""

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.browser_environments import (
    STATUS_CHANGED_EVENT,
    EnvironmentBrowserGate,
    EnvironmentCreationDisabledError,
    EnvironmentExistsError,
    EnvironmentNotFoundError,
    EnvironmentRegistry,
    FakeEnvironmentProvisioner,
    MemoryEnvironmentStore,
    ProvisioningResult,
    StaleEnvironmentError,
    browser_environment_status,
)
from htco_application.execution.contracts import ChannelBinding
from htco_application.execution.gateway import ActionRef, ActionRequest, Channel
from htco_application.execution.router import ExecutionRouter, NoRoute, Route
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import ConsentRecord, EnvironmentStatus, HealthReport
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.state_machine import IllegalTransition
from tests.execution_fakes import Capabilities, FakeChannel, real_catalog

_E = EnvironmentStatus
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("5a000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("5b000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("5a300000-0000-0000-0000-000000000001")
OTHER_SHOP = uuid.UUID("5b300000-0000-0000-0000-000000000001")
USER = uuid.UUID("5a100000-0000-0000-0000-000000000001")
OWNER = ActorRef(ActorKind.USER, str(USER))
SYSTEM = ActorRef(ActorKind.SYSTEM, "browser-health")


@dataclass
class Shops:
    owners: dict[uuid.UUID, uuid.UUID] = field(
        default_factory=lambda: {SHOP: ORG, OTHER_SHOP: OTHER_ORG}
    )

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return self.owners.get(shop_id)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def consent() -> ConsentRecord:
    return ConsentRecord(text_version="v1", user_id=USER, at=NOW, ip_hash="c" * 64)


def registry(
    store: MemoryEnvironmentStore | None = None, *, enabled: bool = True
) -> tuple[EnvironmentRegistry, MemoryEnvironmentStore]:
    store = store or MemoryEnvironmentStore()
    return EnvironmentRegistry(store, Shops(), Clock(), creation_enabled=enabled), store


def created(reg: EnvironmentRegistry) -> uuid.UUID:
    return reg.create(SHOP, by=OWNER, consent=consent(), correlation_id="corr-create").id


def move(
    reg: EnvironmentRegistry,
    environment_id: uuid.UUID,
    *path: EnvironmentStatus,
    start: EnvironmentStatus = _E.PROVISIONING,
) -> None:
    current = start
    for target in path:
        reg.transition(
            environment_id, target, reason=f"to_{target.value}", by=OWNER, expected=current,
            correlation_id=f"corr-{target.value}",
        )  # fmt: skip
        current = target


# --- Creation -----------------------------------------------------------------------------------


@pytest.mark.ac("AC-5.16.4")
def test_create__flag_off__is_refused_and_writes_nothing() -> None:
    reg, store = registry(enabled=False)

    with pytest.raises(EnvironmentCreationDisabledError):
        created(reg)

    assert (store.environments, store.changes, store.events) == ({}, [], [])


@pytest.mark.ac("AC-5.16.4")
def test_shop_without_environment__is_not_configured() -> None:
    reg, _ = registry(enabled=False)

    assert reg.get(SHOP) is None
    assert reg.status_of(SHOP) is _E.NOT_CONFIGURED


@pytest.mark.ac("AC-5.16.2")
def test_create__provisions_and_records_the_first_change() -> None:
    reg, store = registry()

    environment = reg.create(SHOP, by=OWNER, consent=consent(), correlation_id="corr-create")

    assert (environment.status, environment.organization_id, environment.consent) == (
        _E.PROVISIONING,
        ORG,
        consent(),
    )
    [change] = store.changes
    assert (change.from_status, change.to_status, change.actor, change.correlation_id) == (
        _E.NOT_CONFIGURED,
        _E.PROVISIONING,
        OWNER,
        "corr-create",
    )
    [event] = store.events
    assert event.payload["event_type"] == STATUS_CHANGED_EVENT
    assert (event.organization_id, event.shop_id, event.correlation_id) == (
        ORG,
        SHOP,
        "corr-create",
    )


def test_create__twice__is_refused() -> None:
    reg, store = registry()
    created(reg)

    with pytest.raises(EnvironmentExistsError):
        created(reg)

    assert len(store.environments) == 1


def test_create__unknown_shop__is_not_found() -> None:
    reg, _ = registry()

    with pytest.raises(ShopNotFoundError):
        reg.create(uuid.uuid4(), by=OWNER, consent=consent(), correlation_id="c")


def test_create__concurrently__one_environment_per_shop() -> None:
    reg, store = registry()
    results: list[str] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        try:
            created(reg)
            results.append("created")
        except EnvironmentExistsError:
            results.append("exists")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["created"] + ["exists"] * 7
    assert len(store.environments) == 1


def test_create__after_deletion__provisions_the_same_row_again() -> None:
    reg, store = registry()
    environment_id = created(reg)
    move(reg, environment_id, _E.NOT_CONFIGURED)

    again = created(reg)

    assert again == environment_id
    assert [(c.from_status, c.to_status) for c in store.changes] == [
        (_E.NOT_CONFIGURED, _E.PROVISIONING),
        (_E.PROVISIONING, _E.NOT_CONFIGURED),
        (_E.NOT_CONFIGURED, _E.PROVISIONING),
    ]


# --- Transitions --------------------------------------------------------------------------------


@pytest.mark.ac("AC-5.16.1")
def test_transition__the_happy_path__walks_the_table() -> None:
    reg, store = registry()
    environment_id = created(reg)

    move(reg, environment_id, _E.READY, _E.DEGRADED, _E.READY, _E.LOGIN_REQUIRED, _E.SUSPENDED,
         _E.READY)  # fmt: skip

    assert reg.status_of(SHOP) is _E.READY
    assert len(store.changes) == 7


@pytest.mark.ac("AC-5.16.1")
def test_transition__illegal_edge__is_refused_and_records_nothing() -> None:
    reg, store = registry()
    environment_id = created(reg)

    with pytest.raises(IllegalTransition):
        move(reg, environment_id, _E.SUSPENDED)

    assert reg.status_of(SHOP) is _E.PROVISIONING
    assert len(store.changes) == len(store.events) == 1


def test_transition__unexpected_current_status__is_stale() -> None:
    reg, store = registry()
    environment_id = created(reg)

    with pytest.raises(StaleEnvironmentError) as raised:
        reg.transition(
            environment_id, _E.DEGRADED, reason="health_failed", by=SYSTEM, expected=_E.READY,
            correlation_id="c",
        )  # fmt: skip

    assert (raised.value.expected, raised.value.actual) == (_E.READY, _E.PROVISIONING)
    assert len(store.changes) == 1


def test_transition__moved_meanwhile__is_stale() -> None:
    """The store's compare-and-set: two writers that both read PROVISIONING, one wins."""
    reg, store = registry()
    environment_id = created(reg)
    before = store.environments[environment_id]
    move(reg, environment_id, _E.READY)

    with pytest.raises(StaleEnvironmentError):
        reg._move(before, _E.FAILED, by=SYSTEM, reason="late", correlation_id="c")

    assert reg.status_of(SHOP) is _E.READY


def test_transition__unknown_environment__is_not_found() -> None:
    reg, _ = registry()

    with pytest.raises(EnvironmentNotFoundError):
        move(reg, uuid.uuid4(), _E.READY)


@pytest.mark.ac("AC-5.16.2")
def test_every_change__has_actor_reason_correlation_and_one_event() -> None:
    reg, store = registry()
    environment_id = created(reg)

    move(reg, environment_id, _E.READY, _E.SUSPENDED, _E.READY, _E.NOT_CONFIGURED)

    assert len(store.changes) == len(store.events) == 5
    assert all(c.actor == OWNER and c.reason and c.correlation_id for c in store.changes)
    assert len({e.idempotency_key for e in store.events}) == 5
    assert [(e.payload["from_status"], e.payload["to_status"]) for e in store.events] == [
        (c.from_status.value, c.to_status.value) for c in store.changes
    ]


def test_status_event__carries_ids_and_statuses_only() -> None:
    reg, store = registry()
    environment_id = created(reg)
    move(reg, environment_id, _E.FAILED)

    event = store.events[-1]

    assert set(event.payload) == {
        "event_type", "environment_id", "shop_id", "from_status", "to_status", "actor",
        "occurred_at",
    }  # fmt: skip
    assert "to_failed" not in str(event.payload)  # the reason text stays in the history


# --- Health and provisioning --------------------------------------------------------------------


def test_record_health__keeps_the_report_without_changing_the_status() -> None:
    reg, store = registry()
    environment_id = created(reg)
    move(reg, environment_id, _E.READY)
    report = HealthReport(ok=False, checked_at=NOW, problems=("egress_ip_mismatch",))

    reg.record_health(environment_id, report, by=SYSTEM, correlation_id="corr-health")

    assert store.health == [(environment_id, report, SYSTEM, "corr-health")]
    environment = reg.get(SHOP)
    assert environment is not None
    assert (environment.status, environment.last_health_ok, environment.last_health_at) == (
        _E.READY,
        False,
        NOW,
    )


def test_record_health__unknown_environment__is_not_found() -> None:
    reg, _ = registry()

    with pytest.raises(EnvironmentNotFoundError):
        reg.record_health(
            uuid.uuid4(), HealthReport(ok=True, checked_at=NOW), by=SYSTEM, correlation_id="c"
        )


@pytest.mark.anyio
async def test_fake_provisioner__answers_from_its_script() -> None:
    environment_id = uuid.uuid4()
    provisioner = FakeEnvironmentProvisioner(
        results={environment_id: ProvisioningResult(_E.LOGIN_REQUIRED, "credentials_missing")}
    )

    scripted = await provisioner.provision(environment_id)
    default = await provisioner.provision(uuid.uuid4())
    await provisioner.deprovision(environment_id)

    assert (scripted.status, default.status) == (_E.LOGIN_REQUIRED, _E.READY)
    assert provisioner.deprovisioned == [environment_id]


@pytest.mark.anyio
async def test_provisioning_result__drives_the_registry() -> None:
    reg, _ = registry()
    environment_id = created(reg)

    result = await FakeEnvironmentProvisioner().provision(environment_id)
    reg.transition(environment_id, result.status, reason=result.reason, by=SYSTEM,
                   expected=_E.PROVISIONING, correlation_id="corr-provision")  # fmt: skip

    assert reg.status_of(SHOP) is _E.READY


@pytest.mark.parametrize("status", [_E.NOT_CONFIGURED, _E.PROVISIONING, _E.DEGRADED])
def test_provisioning_result__ends_ready_login_required_or_failed(status: _E) -> None:
    with pytest.raises(ValueError, match="provisioning ends"):
        ProvisioningResult(status, "x")


# --- The router's gate --------------------------------------------------------------------------

BROWSER = ChannelBinding(Channel.BROWSER, recipe="read_receipts", browser_scope_id="B-02")
API = ChannelBinding(Channel.API, recipe="list_receipts", requires_capability="receipts_read")


def receipts_request(shop: uuid.UUID = SHOP) -> ActionRequest:
    return ActionRequest(
        action=ActionRef("etsy.receipts.read", 1),
        actor=ActorRef(ActorKind.AUTOMATION, "etsy-order-intake"),
        organization_id=ORG,
        idempotency_key="k",
        correlation_id="c",
        shop_id=shop,
    )


@pytest.mark.ac("AC-5.16.4")
def test_gate__without_environment__api_actions_route_unchanged() -> None:
    reg, _ = registry(enabled=False)
    definition = replace(real_catalog().actions["etsy.receipts.read"], bindings=(API, BROWSER))
    router = ExecutionRouter(
        {Channel.API: FakeChannel(), Channel.BROWSER: FakeChannel(Channel.BROWSER)},
        Capabilities({(SHOP, "receipts_read")}),
        EnvironmentBrowserGate(reg),
    )

    route = router.choose(definition, receipts_request())

    assert isinstance(route, Route)
    assert route.binding is API


@pytest.mark.ac("AC-5.16.4")
@pytest.mark.parametrize("ready", [False, True])
def test_gate__browser_binding__needs_ready_and_stays_off_until_8a(ready: bool) -> None:
    reg, _ = registry()
    environment_id = created(reg)
    if ready:
        move(reg, environment_id, _E.READY)
    definition = replace(real_catalog().actions["etsy.receipts.read"], bindings=(BROWSER,))
    router = ExecutionRouter(
        {Channel.BROWSER: FakeChannel(Channel.BROWSER)}, Capabilities(), EnvironmentBrowserGate(reg)
    )

    route = router.choose(definition, receipts_request())

    assert isinstance(route, NoRoute)
    assert route.code == ("flag_off" if ready else "environment_not_ready")


# --- Reading, for the office --------------------------------------------------------------------


@dataclass
class Reader:
    statuses: Mapping[uuid.UUID, EnvironmentStatus] = field(default_factory=dict)
    seen: list[Mapping[str, Any]] = field(default_factory=list)

    def status_for_user(
        self, claims: Mapping[str, Any], shop_id: uuid.UUID
    ) -> EnvironmentStatus | None:
        self.seen.append(claims)
        return self.statuses.get(shop_id)


def auth(role: Role, *, shops: frozenset[uuid.UUID] = frozenset({SHOP})) -> AuthContext:
    return AuthContext(
        user_id=USER, assurance=AssuranceLevel.AAL1, session_id=None,
        memberships=(Membership(ORG, role, shops),), claims={"sub": str(USER)},
    )  # fmt: skip


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
def test_status__owner_and_admin__read_it(role: Role) -> None:
    reader = Reader({SHOP: _E.DEGRADED})

    status = browser_environment_status(auth(role), reader, Shops(), SHOP, now=NOW)

    assert status is _E.DEGRADED
    assert reader.seen == [{"sub": str(USER)}]


@pytest.mark.ac("AC-5.16.4")
def test_status__no_environment__is_not_configured() -> None:
    status = browser_environment_status(auth(Role.OWNER), Reader(), Shops(), SHOP, now=NOW)

    assert status is _E.NOT_CONFIGURED


@pytest.mark.parametrize(
    "role", [Role.OPERATIONS, Role.FINANCE, Role.SUPPORT, Role.DEVELOPER, Role.VIEWER]
)
def test_status__other_roles__are_refused(role: Role) -> None:
    reader = Reader({SHOP: _E.READY})

    with pytest.raises(ShopAccessDeniedError):
        browser_environment_status(auth(role), reader, Shops(), SHOP, now=NOW)

    assert reader.seen == []


@pytest.mark.ac("AC-5.16.3")
@pytest.mark.parametrize("shop", [OTHER_SHOP, uuid.UUID(int=7)])
def test_status__another_organizations_or_unknown_shop__is_not_found(
    shop: uuid.UUID,
) -> None:
    reader = Reader({OTHER_SHOP: _E.READY})

    with pytest.raises(ShopNotFoundError):
        browser_environment_status(auth(Role.OWNER), reader, Shops(), shop, now=NOW)

    assert reader.seen == []


def test_status__shop_not_granted__is_not_found() -> None:
    with pytest.raises(ShopNotFoundError):
        browser_environment_status(
            auth(Role.OPERATIONS, shops=frozenset()), Reader(), Shops(), SHOP, now=NOW
        )
