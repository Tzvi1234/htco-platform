"""Least privilege for buyer data (ADR-0018, ER-SEC-02; audit AUD-06 in #134).

`etsy.receipts.read` has an order-history form: whole receipts, with shipments and items. Only
Etsy Order Intake receives the buyer's name and shipping address in it. Every other automation
that may call the action (today Order Deduplication & Verification and Tracking Sync), and any
automation added later, receives the same receipts without them. Invented values only."""

import json
import tomllib
import uuid
from pathlib import Path

import pytest

from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.contracts import ChannelBinding
from htco_application.execution.gateway import ActionRequest, Channel
from htco_application.jobs import JsonValue
from htco_application.order_intake import RECEIPTS_READ
from htco_application.order_receipts import BUYER_DATA_AUTOMATIONS, receipt_from_json
from htco_domain.actors import ActorKind, ActorRef
from tests.application.test_action_gateway import ORG, SHOP
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.order_intake_fakes import FAKE_LINE, FAKE_NAME, paid_receipt, shipped

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT = 4000000001


def _callers_of_the_receipts_action() -> list[str]:
    callers = []
    for path in sorted((REPO_ROOT / "registry/automations").glob("*.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if RECEIPTS_READ.name in document.get("allowed_actions", []):
            callers.append(str(document["id"]))
    return callers


CALLERS = _callers_of_the_receipts_action()
# Any automation granted the action later must not get buyer data by default either.
CANDIDATES = [*CALLERS, "a-future-automation"]


def test_only_etsy_order_intake__is_entitled_to_buyer_data() -> None:
    assert frozenset({"etsy-order-intake"}) == BUYER_DATA_AUTOMATIONS
    assert "etsy-order-intake" in CALLERS
    assert {"order-dedup-verification", "tracking-sync"} <= set(CALLERS)


async def _order_history(automation_id: str, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    receipt = shipped(paid_receipt(RECEIPT), minutes=30)
    channel = EtsyApiChannel(FakeMarketplace(order_receipts=(receipt,)), EtsyShops())
    outcome = await channel.execute(
        ActionRequest(
            action=RECEIPTS_READ,
            actor=ActorRef(ActorKind.AUTOMATION, automation_id),
            organization_id=ORG,
            shop_id=SHOP,
            idempotency_key=f"{automation_id}:{uuid.uuid4()}",
            correlation_id="c",
            input={"order_history": True, **values},
        ),
        ChannelBinding(Channel.API, recipe="list_receipts"),
    )
    assert outcome.output is not None
    return dict(outcome.output)


@pytest.mark.parametrize("automation_id", CANDIDATES)
@pytest.mark.parametrize("values", [{"receipt_id": RECEIPT}, {"limit": 100, "offset": 0}])
async def test_order_history_form__buyer_name_and_address_reach_etsy_order_intake_only(
    automation_id: str, values: dict[str, JsonValue]
) -> None:
    output = await _order_history(automation_id, values)
    receipts = output["receipts"]
    assert isinstance(receipts, list)
    [raw] = receipts
    assert isinstance(raw, dict)
    text = json.dumps(output)
    receipt = receipt_from_json(raw)

    if automation_id in BUYER_DATA_AUTOMATIONS:
        assert receipt.buyer_name == FAKE_NAME
        assert receipt.shipping_address is not None
    else:
        assert raw["buyer_name"] is None
        assert raw["shipping_address"] is None
        assert FAKE_NAME not in text
        assert FAKE_LINE not in text
        assert receipt.buyer_display_name is None
    # The order facts every caller needs stay: items, totals and shipments with tracking codes.
    assert receipt.receipt_id == RECEIPT
    assert receipt.lines
    assert receipt.shipments
