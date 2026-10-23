"""The Orders screen's bulk actions (slice 5.7, the owner's decision of 2026-10-23): a CSV export
of the filtered orders, audited, with buyer columns only for buyer-data roles and formulas
escaped; and "Re-sync selected", one queued Etsy Order Intake re-read per receipt, rate-limited
and audited. All values invented."""

import csv
import io
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from htco_application.audit import AuditRecord
from htco_application.etsy_webhooks import WindowLimiter
from htco_application.order_bulk import (
    BUYER_COLUMNS,
    MAX_RESYNC,
    ExportUnavailableError,
    OrdersForbiddenError,
    ResyncRateLimitedError,
    ResyncUnavailableError,
    export_orders,
    resync_orders,
)
from htco_application.shop_dashboard import OrderFilter, OrderPeriod
from htco_application.shop_integrations import ShopIntegration
from htco_application.shops import ShopNotFoundError
from htco_domain.identity import Role
from tests.application.test_order_history import FAKE_LINE, FULL_NAME, Setup, many
from tests.application.test_shop_dashboard import OTHER_ORG, SHOP, auth
from tests.order_history_fakes import AuditLog
from tests.shop_data_fakes import NOW, line

pytestmark = pytest.mark.anyio


@dataclass
class Queue:
    asked: list[tuple[uuid.UUID, tuple[int, ...], str]] = field(default_factory=list)

    async def enqueue_receipts(
        self,
        integration: ShopIntegration,
        receipt_ids: tuple[int, ...],
        *,
        now: datetime,  # noqa: ARG002 - the port
        correlation_id: str,
    ) -> int:
        self.asked.append((integration.shop_id, receipt_ids, correlation_id))
        return len(receipt_ids)


async def stored(count: int = 3) -> tuple[Setup, AuditLog]:
    setup = Setup(many(count))
    await setup.sync.run(setup.integration)
    audit = AuditLog()
    setup.dashboard = replace(setup.dashboard, audit=audit)
    return setup, audit


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


# --- export ---------------------------------------------------------------------------------


async def test_export__owner__every_order_with_buyer_name_and_address() -> None:
    setup, _ = await stored()

    found = await export_orders(setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)

    assert found.buyer_columns is True
    table = rows(found.text)
    assert [r["order"] for r in table] == ["100000", "100001", "100002"]
    assert table[0]["buyer_name"] == FULL_NAME
    assert table[0]["address_line_1"] == FAKE_LINE
    assert set(BUYER_COLUMNS) <= set(table[0])
    assert found.filename.endswith(".csv")


@pytest.mark.parametrize("role", [Role.FINANCE, Role.VIEWER, Role.DEVELOPER])
async def test_export__roles_without_buyer_data__the_buyer_columns_are_omitted(role: Role) -> None:
    setup, _ = await stored()

    found = await export_orders(setup.dashboard, auth(role), SHOP, now=NOW, period=OrderPeriod.ALL)

    assert found.buyer_columns is False
    assert FULL_NAME not in found.text
    assert FAKE_LINE not in found.text
    assert not set(BUYER_COLUMNS) & set(rows(found.text)[0])
    assert "tracking" not in rows(found.text)[0]


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.SUPPORT])
async def test_export__buyer_data_roles__get_the_buyer_columns(role: Role) -> None:
    setup, _ = await stored()

    found = await export_orders(setup.dashboard, auth(role), SHOP, now=NOW, period=OrderPeriod.ALL)

    assert found.buyer_columns is True


async def test_export__formulas_in_any_cell__escaped() -> None:
    setup, _ = await stored(1)
    key = (SHOP, 100_000)
    setup.store.orders[key] = replace(
        setup.store.orders[key],
        lines=(replace(line(1), title='=HYPERLINK("http://x.invalid","click")', sku="+SKU-1"),),
        buyer_name="@evil",
    )

    found = await export_orders(setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)

    [row] = rows(found.text)
    assert row["items"].startswith("'=HYPERLINK")
    assert row["skus"] == "'+SKU-1"
    assert row["buyer_name"] == "'@evil"


async def test_export__the_filter_and_the_selection__as_on_the_screen() -> None:
    setup, _ = await stored(4)

    chosen = await export_orders(
        setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL, order_ids=(100_001, 100_003)
    )
    shipped = await export_orders(
        setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL, state=OrderFilter.UNPAID
    )

    assert [r["order"] for r in rows(chosen.text)] == ["100001", "100003"]
    assert rows(shipped.text) == []


async def test_export__audited_with_counts_and_no_buyer_data() -> None:
    setup, audit = await stored()

    await export_orders(
        setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL, correlation_id="req-1"
    )

    [record] = audit.records
    assert isinstance(record, AuditRecord)
    assert (record.action, record.entity_id, record.correlation_id) == (
        "orders.exported",
        str(SHOP),
        "req-1",
    )
    assert record.after == {"orders": 3, "period": "all", "state": None, "selected": 0,
                            "buyer_columns": True}  # fmt: skip
    assert FULL_NAME not in str(record)


async def test_export__without_an_audit_log__refused() -> None:
    setup, _ = await stored()
    setup.dashboard = replace(setup.dashboard, audit=None)

    with pytest.raises(ExportUnavailableError):
        await export_orders(setup.dashboard, auth(), SHOP, now=NOW)


async def test_export__another_organization__not_found() -> None:
    setup, audit = await stored()

    with pytest.raises(ShopNotFoundError):
        await export_orders(setup.dashboard, auth(org=OTHER_ORG), SHOP, now=NOW)
    assert audit.records == []


# --- re-sync selected -----------------------------------------------------------------------


def with_queue(setup: Setup, queue: Queue, limit: int = 5) -> None:
    setup.dashboard = replace(
        setup.dashboard,
        resync=queue,
        resync_limiter=WindowLimiter(limit, window=timedelta(minutes=1)),
    )


async def test_resync__one_queued_reread_per_receipt_audited() -> None:
    setup, audit = await stored()
    queue = Queue()
    with_queue(setup, queue)

    queued = await resync_orders(
        setup.dashboard, auth(Role.OPERATIONS), SHOP, (100_000, 100_001, 100_000),
        now=NOW, correlation_id="req-2",
    )  # fmt: skip

    assert queued == 2
    assert queue.asked == [(SHOP, (100_000, 100_001), "req-2")]
    [record] = audit.records
    assert isinstance(record, AuditRecord)
    assert record.action == "orders.resync_requested"
    assert record.after == {"receipt_ids": [100_000, 100_001]}


async def test_resync__over_the_rate__refused_with_the_wait() -> None:
    setup, _ = await stored()
    with_queue(setup, Queue(), limit=1)
    await resync_orders(setup.dashboard, auth(), SHOP, (100_000,), now=NOW, correlation_id="a")

    with pytest.raises(ResyncRateLimitedError) as refused:
        await resync_orders(setup.dashboard, auth(), SHOP, (100_001,), now=NOW, correlation_id="b")

    assert 0 < refused.value.retry_after_seconds <= 60


async def test_resync__too_many_or_none__refused() -> None:
    setup, _ = await stored()
    with_queue(setup, Queue())

    with pytest.raises(ValueError, match="between 1 and"):
        await resync_orders(
            setup.dashboard, auth(), SHOP, tuple(range(1, MAX_RESYNC + 2)), now=NOW,
            correlation_id="c",
        )  # fmt: skip
    with pytest.raises(ValueError, match="between 1 and"):
        await resync_orders(setup.dashboard, auth(), SHOP, (), now=NOW, correlation_id="c")


@pytest.mark.parametrize("role", [Role.VIEWER, Role.FINANCE, Role.SUPPORT])
async def test_resync__roles_that_do_not_retry_syncs__forbidden(role: Role) -> None:
    setup, audit = await stored()
    queue = Queue()
    with_queue(setup, queue)

    with pytest.raises(OrdersForbiddenError):
        await resync_orders(setup.dashboard, auth(role), SHOP, (100_000,), now=NOW,
                            correlation_id="d")  # fmt: skip
    assert queue.asked == []
    assert audit.records == []


async def test_resync__without_the_queue__unavailable() -> None:
    setup, _ = await stored()

    with pytest.raises(ResyncUnavailableError):
        await resync_orders(setup.dashboard, auth(), SHOP, (100_000,), now=NOW, correlation_id="e")


async def test_resync__another_organization__not_found() -> None:
    setup, _ = await stored()
    queue = Queue()
    with_queue(setup, queue)

    with pytest.raises(ShopNotFoundError):
        await resync_orders(setup.dashboard, auth(org=OTHER_ORG), SHOP, (100_000,), now=NOW,
                            correlation_id="f")  # fmt: skip
    assert queue.asked == []
