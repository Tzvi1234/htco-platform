"""A browser challenge handed to a person and resumed, on the Postgres action store (5.13)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from htco_application.execution.channels import FakeBrowserChannel, MemoryHumanTasks, RecipeOutcome
from htco_application.execution.gateway import ActionOutcome, Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_persistence.actions import PostgresActionStore
from htco_persistence.database import Database
from tests.application.test_action_gateway import office
from tests.application.test_execution_router import BROWSER, Browser, receipts_action
from tests.execution_fakes import Approvals, Capabilities, Principals, Shops
from tests.integration.test_action_store import SHOPS, ask, shop_of
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.mark.ac("AC-5.13.4")
async def test_challenge__handed_off_then_resumed_with_the_persons_result(admin: Database) -> None:
    tenant = Tenant(admin)
    shop = shop_of(admin, tenant)
    registry = office()
    human = MemoryHumanTasks()
    browser = FakeBrowserChannel({"read_receipts": RecipeOutcome.challenge()})
    service = ActionGatewayService(
        GatewayParts(
            catalog=receipts_action(BROWSER),
            registry=lambda: registry,
            store=PostgresActionStore(admin),
            principals=Principals(),
            shops=Shops(),
            approvals=Approvals(),
            adapters={Channel.BROWSER: browser},
            capabilities=Capabilities(),
            clock=lambda: datetime.now(UTC),
            browser=Browser(scopes={(shop, "etsy.receipts.read"): datetime.now(UTC)}),
            human=human,
        )
    )
    request = ask(tenant, shop_id=SHOPS[tenant.organization_id])

    handed = await service.submit(request)
    waiting = await service.submit(request)
    [task_id] = human.tasks
    human.complete(task_id, {"receipts": [], "count": 0})
    done = await service.submit(request)

    assert handed.outcome is ActionOutcome.HANDED_TO_HUMAN
    assert (waiting.outcome, waiting.channel) == (ActionOutcome.HANDED_TO_HUMAN, Channel.BROWSER)
    assert (done.outcome, done.channel) == (ActionOutcome.SUCCEEDED, Channel.BROWSER)
    assert len(browser.calls) == 1
    assert (
        scalar(admin, "select state from app.action_executions where id = %s", [done.execution_id])
        == "succeeded"
    )
