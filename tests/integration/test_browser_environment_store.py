"""The Shop Environment Registry against real Postgres (slice 5.16): the store's transactions,
the database's transition trigger, the history and events, and the Owner's read through RLS."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import psycopg
import pytest

from htco_application.browser_environments import (
    STATUS_CHANGED_EVENT,
    EnvironmentRegistry,
    StaleEnvironmentError,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import ConsentRecord, EnvironmentStatus, HealthReport
from htco_persistence.browser_environments import (
    PostgresEnvironmentReader,
    PostgresEnvironmentStore,
)
from htco_persistence.database import Database
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

_E = EnvironmentStatus


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def a_shop(admin: Database, tenant: Tenant) -> uuid.UUID:
    shop = uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Browser test shop', 'etsy', 'active')",
            [shop, tenant.organization_id],
        )
    return shop


def registry(admin: Database) -> EnvironmentRegistry:
    store = PostgresEnvironmentStore(admin)
    return EnvironmentRegistry(store, store, lambda: datetime.now(UTC), creation_enabled=True)


def create(reg: EnvironmentRegistry, tenant: Tenant, shop: uuid.UUID) -> uuid.UUID:
    consent = ConsentRecord(
        text_version="v1", user_id=tenant.owner, at=datetime.now(UTC), ip_hash="d" * 64
    )
    by = ActorRef(ActorKind.USER, str(tenant.owner))
    return reg.create(shop, by=by, consent=consent, correlation_id="corr-db-create").id


def claims(user: uuid.UUID) -> dict[str, str]:
    return {"sub": str(user), "role": "authenticated", "aal": "aal1"}


@pytest.mark.ac("AC-5.16.2")
async def test_transitions__store_history_and_one_event_each(admin: Database) -> None:
    tenant = Tenant(admin)
    shop = a_shop(admin, tenant)
    reg = registry(admin)
    environment_id = create(reg, tenant, shop)
    system = ActorRef(ActorKind.SYSTEM, "browser-health")

    reg.transition(environment_id, _E.READY, reason="preflight_passed", by=system,
                   expected=_E.PROVISIONING, correlation_id="corr-db-ready")  # fmt: skip
    reg.record_health(
        environment_id,
        HealthReport(ok=False, checked_at=datetime.now(UTC), problems=("disk_low",)),
        by=system,
        correlation_id="corr-db-health",
    )

    assert reg.status_of(shop) is _E.READY
    with admin.connection() as connection:
        history = connection.execute(
            """
            select kind, from_status, to_status, actor_kind, reason, correlation_id
            from app.browser_environment_events where environment_id = %s order by occurred_at
            """,
            [environment_id],
        ).fetchall()
        events = connection.execute(
            "select payload ->> 'to_status' from jobs.outbox "
            "where payload ->> 'event_type' = %s and payload ->> 'environment_id' = %s "
            "order by created_at",
            [STATUS_CHANGED_EVENT, str(environment_id)],
        ).fetchall()
    assert history == [
        ("transition", "not_configured", "provisioning", "user", "created", "corr-db-create"),
        ("transition", "provisioning", "ready", "system", "preflight_passed", "corr-db-ready"),
        ("health", None, None, "system", None, "corr-db-health"),
    ]
    assert events == [("provisioning",), ("ready",)]


@pytest.mark.ac("AC-5.16.1")
async def test_database__refuses_an_edge_outside_the_table(admin: Database) -> None:
    tenant = Tenant(admin)
    shop = a_shop(admin, tenant)
    environment_id = create(registry(admin), tenant, shop)

    with pytest.raises(psycopg.errors.CheckViolation), admin.connection() as connection:
        connection.execute(
            "update app.shop_browser_environments set status = 'suspended' where id = %s",
            [environment_id],
        )

    assert (
        scalar(
            admin,
            "select status from app.shop_browser_environments where id = %s",
            [environment_id],
        )
        == "provisioning"
    )


async def test_store__compare_and_set__a_stale_writer_loses(admin: Database) -> None:
    tenant = Tenant(admin)
    shop = a_shop(admin, tenant)
    reg = registry(admin)
    environment_id = create(reg, tenant, shop)
    store = PostgresEnvironmentStore(admin)
    before = store.by_id(environment_id)
    assert before is not None
    reg.transition(environment_id, _E.FAILED, reason="provisioning_failed",
                   by=ActorRef(ActorKind.SYSTEM, "x"), expected=_E.PROVISIONING,
                   correlation_id="c")  # fmt: skip

    with pytest.raises(StaleEnvironmentError):
        reg._move(before, _E.READY, by=ActorRef(ActorKind.SYSTEM, "y"), reason="late",
                  correlation_id="c")  # fmt: skip

    assert reg.status_of(shop) is _E.FAILED


@pytest.mark.ac("AC-5.16.3")
async def test_reader__an_owner_sees_their_shop_only(admin: Database) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    shop, other_shop = a_shop(admin, tenant), a_shop(admin, other)
    reg = registry(admin)
    create(reg, tenant, shop)
    reader = PostgresEnvironmentReader(admin)

    own = reader.status_for_user(claims(tenant.owner), shop)
    foreign = reader.status_for_user(claims(other.owner), shop)
    none_yet = reader.status_for_user(claims(other.owner), other_shop)

    assert (own, foreign, none_yet) == (_E.PROVISIONING, None, None)
