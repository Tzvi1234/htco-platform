"""Notifications on real Postgres (slice 6.4), as the worker in its job's organization: the
owners found through the worker's function, one notification and one email per recipient however
often a request runs, and the address read at send time."""

import uuid
from collections.abc import AsyncIterator

import pytest

from htco_application.notification_preferences import PreferenceSet
from htco_application.notifications import NotificationRequest, NotificationService
from htco_domain.notifications import NotificationKind, NotificationSeverity, Preference
from htco_persistence.database import Database, tenant_scope
from htco_persistence.notifications import (
    PostgresEmailDirectory,
    PostgresNotificationPreferences,
    PostgresNotificationStore,
)
from tests.integration.test_etsy_connection import NOW, _url, scalar
from tests.integration.test_order_history import connected

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.mark.ac("AC-6.4.1")
async def test_worker__one_notification_and_one_email_per_owner_however_often(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org = tenant.organization_id
    service = NotificationService(PostgresNotificationStore(worker_db), clock=lambda: NOW)
    request = NotificationRequest(
        org, NotificationKind.INCIDENT_OPENED, f"incident_opened:{uuid.uuid4()}",
        {"receipt_id": 7, "cause": "tracking_missing", "severity": "medium"},
        shop_id=integration.shop_id,
    )  # fmt: skip

    with tenant_scope(org):
        created = [await service.notify(request) for _ in range(3)]
        owner = scalar(
            database,
            "select user_id from app.memberships where organization_id = %s and role = 'owner'",
            [org],
        )
        address = PostgresEmailDirectory(worker_db).address_of(org, owner)

    assert created[0] >= 1
    assert created[1:] == [0, 0]
    rows = scalar(
        database,
        "select count(*) from app.notifications where organization_id = %s and dedup_key = %s",
        [org, request.dedup_key],
    )
    emails = scalar(
        database,
        "select count(*) from jobs.outbox where destination = 'email.send'"
        " and payload->>'user_id' = %s and idempotency_key like %s",
        [str(owner), f"email:{request.dedup_key}:%"],
    )
    assert rows == created[0]
    assert emails == 1
    assert address is not None
    assert "@" in address


@pytest.mark.ac("AC-7.6.1")
async def test_preferences__stored_read_back_and_a_muted_shop_skips_its_owner(
    database: Database, worker_db: Database
) -> None:
    """Slice 7.6 on Postgres: the API's store replaces and reads the owner's choices; the worker's
    recipient query sees the mute, so a non-critical alert skips that owner, a critical one not."""
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    owner = scalar(
        database,
        "select user_id from app.memberships where organization_id = %s and role = 'owner'",
        [org],
    )
    preferences = PostgresNotificationPreferences(database)
    wanted = PreferenceSet(
        kinds={NotificationKind.ALERT_LOW_REVIEW: Preference(in_app=True, email=False)},
        severities={NotificationSeverity.LOW: Preference(in_app=False, email=False)},
        muted_shops=frozenset({shop}),
    )
    preferences.replace(org, owner, wanted, now=NOW)
    preferences.replace(org, owner, wanted, now=NOW)  # replacing twice stores one set
    assert preferences.read(org, owner) == wanted

    service = NotificationService(PostgresNotificationStore(worker_db), clock=lambda: NOW)
    high = NotificationRequest(
        org, NotificationKind.ALERT_SHIP_BY, f"alert_ship_by:{uuid.uuid4()}",
        {"receipt_id": 7, "ship_by": "2026-10-30"}, shop_id=shop,
    )  # fmt: skip
    critical = NotificationRequest(
        org, NotificationKind.INCIDENT_OPENED, f"incident_opened:{uuid.uuid4()}",
        {"receipt_id": 8, "cause": "tracking_missing", "severity": "critical"}, shop_id=shop,
    )  # fmt: skip
    with tenant_scope(org):
        await service.notify(high)
        await service.notify(critical)

    def owner_rows(key: str) -> int:
        return int(
            scalar(
                database,
                "select count(*) from app.notifications where organization_id = %s"
                " and user_id = %s and dedup_key = %s",
                [org, owner, key],
            )
        )

    assert owner_rows(high.dedup_key) == 0
    assert owner_rows(critical.dedup_key) == 1
