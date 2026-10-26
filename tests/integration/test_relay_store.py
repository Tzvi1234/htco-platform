"""The AutoVA relay's store against Postgres (slice 6.10): one item per order, a number for one
order of a shop, the code kept for the copy only and deleted on close, the worker in its job's
organization."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from htco_application.audit import AuditRecord
from htco_application.relay import DuplicateTrackingError, RelayItem, RelayItemMovedError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import tracking_fingerprint
from htco_domain.relay import ClosedReason, RelayState
from htco_persistence.database import Database, tenant_scope
from htco_persistence.relay import PostgresRelayStore
from tests.integration.test_etsy_connection import NOW, _url, scalar
from tests.integration.test_order_history import connected

pytestmark = [pytest.mark.db, pytest.mark.anyio]

CODE = "9400100000000000001234"
OWNER = ActorRef(ActorKind.USER, str(uuid.UUID(int=7)))


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def item(organization_id: uuid.UUID, shop_id: uuid.UUID, receipt_id: int) -> RelayItem:
    return RelayItem(
        id=uuid.uuid7(), organization_id=organization_id, shop_id=shop_id,
        receipt_id=receipt_id, state=RelayState.AWAITING_TRACKING,
        paid_at=NOW - timedelta(days=3), opened_at=NOW, updated_at=NOW,
    )  # fmt: skip


def audit(organization_id: uuid.UUID, item_id: uuid.UUID) -> AuditRecord:
    return AuditRecord.new(
        organization_id=organization_id, actor=OWNER, action="relay.tracking_pasted",
        entity_type="relay_item", entity_id=str(item_id),
        after={"carrier": "usps", "last4": "1234", "receipt_id": 61},
    )  # fmt: skip


@pytest.mark.ac("AC-6.10.1")
async def test_one_item_per_order_paste_once_and_close_on_etsy(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    worker, api = PostgresRelayStore(worker_db), PostgresRelayStore(database)
    first = item(org, shop, 61)

    with tenant_scope(org):
        assert worker.open_item(first) == (first, True)
        # One per order, ever: a second opening answers the stored item.
        stored, created = worker.open_item(item(org, shop, 61))
        assert (stored.id, created) == (first.id, False)
        assert worker.open_item(item(org, shop, 62))[1] is True

    fingerprint = tracking_fingerprint(shop, CODE)
    moved = api.hand_off(
        first.id, carrier="usps", fingerprint=fingerprint, last4="1234", code=CODE, now=NOW,
        audit=audit(org, first.id),
    )  # fmt: skip
    assert (moved.state, moved.carrier, moved.last4) == (RelayState.HANDED_OFF, "usps", "1234")
    assert scalar(database, "select code from app.relay_item_codes where relay_item_id = %s",
                  [first.id]) == CODE  # fmt: skip
    audited = (
        "select count(*) from audit.audit_log where entity_type = 'relay_item' and entity_id = %s"
    )
    assert scalar(database, audited, [str(first.id)]) == 1

    with pytest.raises(RelayItemMovedError):
        api.hand_off(
            first.id, carrier="usps", fingerprint=fingerprint, last4="1234", code=CODE,
            now=NOW, audit=audit(org, first.id),
        )  # fmt: skip
    [second] = [i for i in api.open_items(org, shop_ids=None, limit=10) if i.receipt_id == 62]
    with pytest.raises(DuplicateTrackingError):
        api.hand_off(
            second.id, carrier="usps", fingerprint=fingerprint, last4="1234", code=CODE,
            now=NOW, audit=audit(org, second.id),
        )  # fmt: skip
    assert api.item(org, second.id).state is RelayState.AWAITING_TRACKING  # type: ignore[union-attr]

    with tenant_scope(org):
        assert worker.close_order(shop, 61, reason=ClosedReason.TRACKING_ON_ETSY, now=NOW) is True
    closed = api.item(org, first.id)
    assert closed is not None
    assert (closed.state, closed.closed_reason) == (
        RelayState.CLOSED,
        ClosedReason.TRACKING_ON_ETSY,
    )
    assert scalar(database, "select count(*) from app.relay_item_codes where relay_item_id = %s",
                  [first.id]) == 0  # fmt: skip


async def test_the_worker_reaches_its_jobs_organization_only(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    other, _ = await connected(database)
    worker = PostgresRelayStore(worker_db)
    with tenant_scope(tenant.organization_id):
        worker.open_item(item(tenant.organization_id, integration.shop_id, 71))

    with tenant_scope(other.organization_id):
        assert (
            worker.close_order(
                integration.shop_id, 71, reason=ClosedReason.TRACKING_ON_ETSY, now=NOW
            )
            is False
        )
    assert (
        PostgresRelayStore(database)
        .open_items(tenant.organization_id, shop_ids=None, limit=10)[0]
        .state
        is RelayState.AWAITING_TRACKING
    )


async def test_open_items__keyset_pages_reach_every_item(database: Database) -> None:
    """Codex on #159: reconciliation pages through every open item, oldest first."""
    tenant, integration = await connected(database)
    store = PostgresRelayStore(database)
    org, shop = tenant.organization_id, integration.shop_id
    with tenant_scope(org):
        for receipt in (81, 82, 83):
            store.open_item(item(org, shop, receipt))

    first = store.open_items(org, shop_ids=frozenset({shop}), limit=2)
    rest = store.open_items(org, shop_ids=frozenset({shop}), limit=2, after=first[-1])

    assert [i.receipt_id for i in (*first, *rest)] == [81, 82, 83]
    assert store.open_items(org, shop_ids=frozenset({shop}), limit=2, after=rest[-1]) == ()
