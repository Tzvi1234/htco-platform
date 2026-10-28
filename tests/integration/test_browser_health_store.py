"""Health, status and sign-in on real Postgres, on the browser worker's role (slice 8A.3).

The browser worker logs in as `htco_browser_login` and switches to `htco_browser`; in its
organization it records health, moves the health and sign-in edges with their history rows and
status events, and reaches nothing else."""

import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from htco_application.browser_environments import (
    EnvironmentNotFoundError,
    EnvironmentRegistry,
    StaleEnvironmentError,
    status_event,
)
from htco_application.browser_health import EnvironmentHealth, MemorySignInTasks, SessionValidation
from htco_application.messaging import OutboxMessage
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import (
    BrowserEnvironment,
    EnvironmentChange,
    EnvironmentStatus,
    HealthReport,
)
from htco_persistence.browser_environments import (
    BrowserCannotCreateError,
    PostgresBrowserEnvironmentStore,
)
from htco_persistence.database import Database, tenant_scope
from tests.integration.test_browser_profile_store import an_environment
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

WORKER = ActorRef(ActorKind.SYSTEM, "browser-worker")
_E = EnvironmentStatus


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@contextmanager
def login_password(admin: Database) -> Iterator[str]:
    """A throwaway password for the browser's login, for one test; no migration sets one."""
    password = secrets.token_urlsafe(24)
    with admin.connection() as connection:
        connection.execute(
            sql.SQL("alter role htco_browser_login password {}").format(sql.Literal(password))
        )
    try:
        yield make_conninfo(_url(), user="htco_browser_login", password=password)
    finally:
        with admin.connection() as connection:
            connection.execute("alter role htco_browser_login password null")


@pytest.fixture
async def browser(admin: Database) -> AsyncIterator[Database]:
    """The production shape: the browser's own login, switched to `htco_browser`."""
    with login_password(admin) as conninfo:
        db = Database(conninfo, min_size=1, max_size=2, role="htco_browser")
        await db.start()
        try:
            yield db
        finally:
            await db.stop()


def now() -> datetime:
    return datetime.now(UTC)


def ready(admin: Database, tenant: Tenant) -> uuid.UUID:
    _, environment_id = an_environment(admin, tenant)
    with admin.connection() as connection:
        connection.execute(
            "update app.shop_browser_environments set status = 'ready' where id = %s",
            [environment_id],
        )
    return environment_id


def report(*, ok: bool) -> HealthReport:
    return HealthReport(ok=ok, checked_at=now(), problems=() if ok else ("egress_ip_mismatch",))


def events(admin: Database, environment_id: uuid.UUID) -> int:
    return int(
        scalar(
            admin,
            "select count(*) from jobs.outbox where payload->>'environment_id' = %s",
            [str(environment_id)],
        )
    )


@pytest.mark.ac("AC-8A.3.3")
async def test_health__two_failures_degrade_and_two_passes_restore(
    admin: Database, browser: Database
) -> None:
    tenant = Tenant(admin)
    environment_id = ready(admin, tenant)
    store = PostgresBrowserEnvironmentStore(browser)
    registry = EnvironmentRegistry(store, store, now)
    health = EnvironmentHealth(registry, store, WORKER)
    with tenant_scope(tenant.organization_id):
        statuses = []
        for ok in (False, False, True, True):
            environment = store.by_id(environment_id)
            assert environment is not None
            statuses.append(health.record(environment, report(ok=ok), correlation_id="t:1"))
        assert store.recent_health(environment_id, 3) == (True, True, False)
    assert statuses == [_E.READY, _E.DEGRADED, _E.DEGRADED, _E.READY]
    assert scalar(
        admin,
        "select count(*) from app.browser_environment_events "
        "where environment_id = %s and kind = 'transition' and actor_kind = 'system'",
        [environment_id],
    ) == 2  # fmt: skip
    assert events(admin, environment_id) == 2
    assert scalar(
        admin,
        "select count(*) from jobs.jobs j join jobs.outbox o "
        "on j.idempotency_key = 'outbox:' || o.id "
        "where o.payload->>'environment_id' = %s and j.kind = 'outbox.deliver'",
        [str(environment_id)],
    ) == 2  # fmt: skip


@pytest.mark.ac("AC-8A.3.2")
async def test_sign_in__a_signed_out_profile_asks_a_person_once(
    admin: Database, browser: Database
) -> None:
    tenant = Tenant(admin)
    environment_id = ready(admin, tenant)
    store = PostgresBrowserEnvironmentStore(browser)
    tasks = MemorySignInTasks()
    validation = SessionValidation(EnvironmentRegistry(store, store, now), tasks, WORKER)
    with tenant_scope(tenant.organization_id):
        environment = store.by_id(environment_id)
        assert environment is not None
        validation.signed_out(environment, correlation_id="t:2")
        again = store.by_id(environment_id)
        assert again is not None
        validation.signed_out(again, correlation_id="t:2")
        assert again.status is _E.LOGIN_REQUIRED
    assert len(tasks.tasks) == 1
    assert events(admin, environment_id) == 1


async def test_store__one_organization_never_reaches_another(
    admin: Database, browser: Database
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    foreign = ready(admin, other)
    store = PostgresBrowserEnvironmentStore(browser)
    registry = EnvironmentRegistry(store, store, now)
    with tenant_scope(tenant.organization_id):
        assert store.by_id(foreign) is None
        assert store.recent_health(foreign, 2) == ()
        with pytest.raises(EnvironmentNotFoundError):
            registry.transition(foreign, _E.DEGRADED, reason="forged", by=WORKER,
                                expected=_E.READY, correlation_id="t:3")  # fmt: skip
    assert scalar(
        admin, "select status from app.shop_browser_environments where id = %s", [foreign]
    ) == "ready"  # fmt: skip


async def test_store__a_status_that_moved_meanwhile_is_not_overwritten(
    admin: Database, browser: Database
) -> None:
    tenant = Tenant(admin)
    environment_id = ready(admin, tenant)
    shop_id = scalar(
        admin, "select shop_id from app.shop_browser_environments where id = %s", [environment_id]
    )
    store = PostgresBrowserEnvironmentStore(browser)
    registry = EnvironmentRegistry(store, store, now)
    with tenant_scope(tenant.organization_id):
        assert store.organization_of_shop(shop_id) == tenant.organization_id
        assert store.organization_of_shop(uuid.uuid4()) is None
        stale = store.by_shop(shop_id)
        assert stale is not None
        with admin.connection() as connection:
            connection.execute(
                "update app.shop_browser_environments set status = 'suspended' where id = %s",
                [environment_id],
            )
        with pytest.raises(StaleEnvironmentError):
            registry.transition(environment_id, _E.DEGRADED, reason="health_failed_twice",
                                by=WORKER, expected=_E.READY, correlation_id="t:4")  # fmt: skip
        assert not store.apply(
            stale.transition(_E.DEGRADED),
            *_change_and_event(stale),
        )
        with pytest.raises(BrowserCannotCreateError):
            store.insert(stale, *_change_and_event(stale))


def _change_and_event(
    environment: BrowserEnvironment,
) -> tuple[EnvironmentChange, OutboxMessage]:
    change = EnvironmentChange.of(environment, _E.DEGRADED, by=WORKER, reason="test",
                                  correlation_id="t:5", at=now())  # fmt: skip
    return change, status_event(change)


async def test_login__holds_no_right_before_it_switches_role(admin: Database) -> None:
    with (
        login_password(admin) as conninfo,
        psycopg.connect(conninfo, autocommit=True) as connection,
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("select id from app.shop_browser_environments")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("set role postgres")
