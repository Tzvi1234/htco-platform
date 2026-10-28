"""Notifications on real Postgres (slice 6.4), as the worker in its job's organization: the
owners found through the worker's function, one notification and one email per recipient however
often a request runs, and the address read at send time."""

import uuid
from collections.abc import AsyncIterator

import pytest

from htco_application.email_failures import FailedStatus
from htco_application.notifications import (
    DeliveryLinks,
    NotificationRequest,
    NotificationService,
)
from htco_domain.notifications import NotificationKind
from htco_persistence.database import Database, tenant_scope
from htco_persistence.notifications import (
    PostgresEmailDirectory,
    PostgresFailedEmailReader,
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


def _claims(user_id: uuid.UUID) -> dict[str, str]:
    return {"sub": str(user_id), "role": "authenticated", "aal": "aal1"}


@pytest.mark.ac("AC-6.4.3")
async def test_admin_view__a_permanent_failure_with_its_links_and_a_masked_address(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org = tenant.organization_id
    owner = scalar(
        database,
        "select user_id from app.memberships where organization_id = %s and role = 'owner'",
        [org],
    )
    incident, outbox = uuid.uuid4(), uuid.uuid4()
    with tenant_scope(org):
        address = PostgresEmailDirectory(worker_db).address_of(org, owner)
        PostgresEmailDirectory(worker_db).record_email(
            outbox_id=outbox, organization_id=org, user_id=owner,
            kind=NotificationKind.INCIDENT_OPENED, status="failed", provider_id=None,
            error="resend 422", now=NOW,
            about=DeliveryLinks(integration.shop_id, incident, 51),
        )  # fmt: skip
    reader = PostgresFailedEmailReader(database)

    page = await reader.failed_emails(
        _claims(owner), org, status=FailedStatus.FAILED, before=None, limit=50
    )
    stranger = await reader.failed_emails(
        _claims(uuid.uuid4()), org, status=FailedStatus.FAILED, before=None, limit=50
    )

    [found] = [f for f in page.items if f.reason == "resend 422" and f.incident_id == incident]
    assert (found.shop_id, found.receipt_id, found.recipient_user_id) == (
        integration.shop_id,
        51,
        owner,
    )
    assert address is not None
    local, domain = address.split("@")
    assert found.recipient == f"{local[0]}***@{domain[0]}***.{domain.rsplit('.', 1)[1]}"
    assert address not in repr(page)
    assert stranger.items == ()
