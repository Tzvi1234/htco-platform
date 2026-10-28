"""The write actions console's reads against real Postgres (slice 6.8): the newest execution of
each action on a shop, and the shop's organization and granted Etsy scopes."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from htco_application.execution.gateway import ActionOutcome, ActionResult
from htco_application.execution.service import Finish, request_hash
from htco_domain.actions import ActionState
from htco_persistence.actions import PostgresActionStore, PostgresOwnerWriteHistory
from htco_persistence.database import Database
from htco_persistence.worker_gateway import PostgresShopFacts
from tests.integration.test_action_store import NOW, ask, shop_of
from tests.integration.test_etsy_connection import Tenant, _url

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


async def test_last_runs__the_newest_execution_of_each_action_on_the_shop(admin: Database) -> None:
    tenant = Tenant(admin)
    shop = shop_of(admin, tenant)
    store = PostgresActionStore(admin)
    ids = []
    for minutes, outcome in ((0, ActionOutcome.DENIED), (1, ActionOutcome.SUCCEEDED)):
        request = ask(tenant, shop_id=shop)
        claim = store.claim(
            request, request_hash=request_hash(request), now=NOW + timedelta(minutes=minutes)
        )
        state = ActionState.DENIED if outcome is ActionOutcome.DENIED else ActionState.SUCCEEDED
        store.record(
            claim.execution_id,
            Finish(
                state=state,
                path=(ActionState.RECEIVED, state)
                if state is ActionState.DENIED
                else (ActionState.RECEIVED, ActionState.AUTHORIZED, ActionState.ROUTED,
                      ActionState.RUNNING, ActionState.VERIFYING, state),
                result=ActionResult(claim.execution_id, outcome, attempts=1, verified=True,
                                    output={"count": 1}),
            ),
            now=NOW + timedelta(minutes=minutes),
        )  # fmt: skip
        ids.append(claim.execution_id)

    runs = PostgresOwnerWriteHistory(admin).last_runs(
        tenant.organization_id, shop, frozenset({"etsy.receipts.read", "etsy.shop.update"})
    )

    assert set(runs) == {"etsy.receipts.read"}
    last = runs["etsy.receipts.read"]
    assert (last.execution_id, last.outcome, last.verified) == (ids[1], "succeeded", True)
    assert last.output == {"count": 1}
    assert (
        PostgresOwnerWriteHistory(admin).last_runs(
            tenant.organization_id, uuid.uuid4(), frozenset({"etsy.receipts.read"})
        )
        == {}
    )


async def test_shop_facts__the_organization_and_no_scopes_without_a_connection(
    admin: Database,
) -> None:
    tenant = Tenant(admin)
    shop = shop_of(admin, tenant)
    facts = PostgresShopFacts(admin)

    assert facts.organization_of_shop(shop) == tenant.organization_id
    assert facts.organization_of_shop(uuid.uuid4()) is None
    assert facts.scopes_of(shop) == frozenset()  # not connected: every scope is missing
