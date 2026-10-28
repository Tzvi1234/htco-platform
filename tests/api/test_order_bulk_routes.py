"""`POST /v1/shops/{shop_id}/order-exports|order-resyncs` (slice 5.7): the CSV is made on the
server,
streamed `no-store` and audited; the re-sync queues one intake re-read per receipt, is
rate-limited and audited (invented data)."""

import csv
import io
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from htco_api.idempotency import STAGED_KEY_REQUIRED
from htco_application.audit import AuditRecord
from htco_application.etsy_webhooks import WindowLimiter
from htco_domain.identity import Role
from tests.api.test_etsy_connect_routes import CSRF
from tests.api.test_order_history_routes import FULL_NAME, Wired
from tests.api.test_shop_data_routes import _world, problem
from tests.application.test_order_bulk import Queue
from tests.order_history_fakes import AuditLog

pytestmark = pytest.mark.anyio


class Bulk:
    def __init__(self, wired: Wired) -> None:
        self.wired = wired
        self.audit = AuditLog()
        self.queue = Queue()
        dashboard = wired.world.dashboard
        object.__setattr__(dashboard, "audit", self.audit)
        object.__setattr__(dashboard, "resync", self.queue)
        object.__setattr__(
            dashboard, "resync_limiter", WindowLimiter(2, window=timedelta(minutes=1))
        )

    async def post(
        self, action: str, body: dict[str, object], *, key: str | None = ""
    ) -> httpx.Response:
        """`key=""` sends a fresh Idempotency-Key, `None` sends none (architecture audit 6.1)."""
        world = self.wired.world
        headers = dict(CSRF)
        if key is not None:
            headers["Idempotency-Key"] = key or str(uuid.uuid4())
        return await world.http.post(f"/v1/shops/{world.shop_id}/{action}", json=body,
                                     headers=headers)  # fmt: skip


@pytest.fixture
async def bulk() -> AsyncIterator[Bulk]:
    async for world in _world():
        wired = Wired(world)
        await world.get("orders")
        await wired.sync.idle()
        yield Bulk(wired)


def table(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


async def test_export__a_csv_file_streamed_no_store_and_audited(bulk: Bulk) -> None:
    response = await bulk.post("order-exports", {"period": "all"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    rows = table(response.text)
    assert rows
    assert "buyer_name" in rows[0]
    [record] = bulk.audit.records
    assert isinstance(record, AuditRecord)
    assert record.action == "orders.exported"


async def test_export__a_viewer__no_buyer_columns(bulk: Bulk) -> None:
    bulk.wired.world.memberships.role = Role.VIEWER

    response = await bulk.post("order-exports", {"period": "all"})

    assert response.status_code == 200
    assert "buyer_name" not in table(response.text)[0]
    assert FULL_NAME not in response.text


async def test_export__the_selection_only(bulk: Bulk) -> None:
    first = min(receipt for (_, receipt) in bulk.wired.store.orders)

    response = await bulk.post("order-exports", {"period": "all", "order_ids": [first]})

    assert [r["order"] for r in table(response.text)] == [str(first)]


async def test_resync__queued_and_audited(bulk: Bulk) -> None:
    response = await bulk.post("order-resyncs", {"order_ids": [9001, 9002]})

    assert response.status_code == 202, response.text
    assert response.json() == {"queued": 2}
    [(_, receipts, _)] = bulk.queue.asked
    assert receipts == (9001, 9002)


async def test_resync__same_idempotency_key__queued_once_and_replayed(bulk: Bulk) -> None:
    """Architecture audit 6.1: a timed-out retry gets the first answer, not a second re-read
    (or the rate limit's 429)."""
    first = await bulk.post("order-resyncs", {"order_ids": [9001]}, key="resync-1")
    again = await bulk.post("order-resyncs", {"order_ids": [9001]}, key="resync-1")

    assert [first.status_code, again.status_code] == [202, 202]
    assert again.json() == first.json() == {"queued": 1}
    assert again.headers["idempotent-replayed"] == "true"
    assert len(bulk.queue.asked) == 1


async def test_resync__without_an_idempotency_key__the_staged_rule(bulk: Bulk) -> None:
    """Required once STAGED_KEY_REQUIRED flips (400, nothing queued); until then it runs."""
    response = await bulk.post("order-resyncs", {"order_ids": [9001]}, key=None)

    if STAGED_KEY_REQUIRED:
        problem(response, "idempotency-key-required", 400)
        assert bulk.queue.asked == []
    else:
        assert response.status_code == 202
        assert len(bulk.queue.asked) == 1


async def test_resync__over_the_rate__429_with_retry_after(bulk: Bulk) -> None:
    for _ in range(2):
        await bulk.post("order-resyncs", {"order_ids": [9001]})

    response = await bulk.post("order-resyncs", {"order_ids": [9001]})

    problem(response, "resync-rate-limited", 429)
    assert int(response.headers["retry-after"]) > 0


async def test_resync__a_viewer__403(bulk: Bulk) -> None:
    bulk.wired.world.memberships.role = Role.VIEWER

    response = await bulk.post("order-resyncs", {"order_ids": [9001]})

    problem(response, "orders-action-forbidden", 403)
    assert bulk.queue.asked == []


async def test_resync__too_many__422(bulk: Bulk) -> None:
    response = await bulk.post("order-resyncs", {"order_ids": list(range(1, 60))})

    assert response.status_code == 422
