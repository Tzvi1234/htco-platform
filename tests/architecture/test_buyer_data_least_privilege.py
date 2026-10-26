"""Least privilege for buyer data (ADR-0018, ER-SEC-02; audit AUD-06 in #134).

`etsy.receipts.read` has an order-history form: whole receipts, with shipments and items. Only
Etsy Order Intake receives the buyer's name and shipping address in it. Every other automation
that may call the action (today Order Deduplication & Verification and Tracking Sync), and any
automation added later, receives the same receipts without them. Invented values only."""

import json
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.contracts import ChannelBinding
from htco_application.execution.gateway import ActionRequest, Channel
from htco_application.jobs import JsonValue
from htco_application.marketplace import MarketplacePermissionError
from htco_application.order_intake import RECEIPTS_READ
from htco_application.order_receipts import (
    BUYER_DATA_AUTOMATIONS,
    TRACKING_CODE_AUTOMATIONS,
    receipt_from_json,
)
from htco_domain.actors import ActorKind, ActorRef
from tests.application.test_action_gateway import ORG, SHOP
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.order_intake_fakes import (
    FAKE_LINE,
    FAKE_NAME,
    FAKE_NOTE,
    FAKE_TRACKING,
    paid_receipt,
    refunded,
    shipped,
)

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
    # Tracking codes can locate the parcel and so the buyer (ADR-0018): Intake stores them and
    # Tracking Sync records them; nothing else needs them.
    assert frozenset({"etsy-order-intake", "tracking-sync"}) == TRACKING_CODE_AUTOMATIONS
    assert "etsy-order-intake" in CALLERS
    assert {"order-dedup-verification", "tracking-sync"} <= set(CALLERS)


async def _order_history(automation_id: str, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    receipt = refunded(shipped(paid_receipt(RECEIPT), minutes=30), minutes=60)
    receipt = replace(receipt, shipments=shipped(receipt, minutes=30).shipments)
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


@pytest.mark.parametrize("automation_id", CANDIDATES)
@pytest.mark.parametrize("values", [{"receipt_id": RECEIPT}, {"limit": 100, "offset": 0}])
async def test_order_history_form__tracking_codes_reach_intake_and_tracking_sync_only(
    automation_id: str, values: dict[str, JsonValue]
) -> None:
    """Order Deduplication & Verification compares totals and items, never shipments (quill on
    #135): it, and any automation added later, gets the shipments without their codes."""
    output = await _order_history(automation_id, values)
    receipts = output["receipts"]
    assert isinstance(receipts, list)
    [raw] = receipts
    assert isinstance(raw, dict)
    receipt = receipt_from_json(raw)

    assert receipt.shipments  # carrier and time stay for every caller
    codes = {shipment.tracking_code for shipment in receipt.shipments}
    if automation_id in TRACKING_CODE_AUTOMATIONS:
        assert codes == {FAKE_TRACKING}
    else:
        assert codes == {""}
        assert FAKE_TRACKING not in json.dumps(output)


@pytest.mark.parametrize("kind", [ActorKind.USER, ActorKind.EMPLOYEE])
async def test_order_history_form__refused_to_a_person_or_an_employee(kind: ActorKind) -> None:
    """The form is for automations only (quill on #135): a user or an AI employee gets 403 and
    Etsy is never called."""
    marketplace = FakeMarketplace(order_receipts=(shipped(paid_receipt(RECEIPT), minutes=30),))
    channel = EtsyApiChannel(marketplace, EtsyShops())
    request = ActionRequest(
        action=RECEIPTS_READ,
        actor=ActorRef(kind, "etsy-order-intake"),  # even under the Intake's name
        organization_id=ORG,
        shop_id=SHOP,
        idempotency_key=f"{kind.value}:{uuid.uuid4()}",
        correlation_id="c",
        input={"order_history": True, "receipt_id": RECEIPT},
    )

    with pytest.raises(MarketplacePermissionError) as refused:
        await channel.execute(request, ChannelBinding(Channel.API, recipe="list_receipts"))

    assert refused.value.status == 403
    assert marketplace.calls == []


@pytest.mark.parametrize("automation_id", CANDIDATES)
async def test_order_history_form__the_sellers_refund_note_reaches_intake_only(
    automation_id: str,
) -> None:
    """The seller's note on a refund is shown only to buyer-data roles (sage on #135): every
    automation but Intake gets the refund's amount, reason and status without it."""
    output = await _order_history(automation_id, {"receipt_id": RECEIPT})
    receipts = output["receipts"]
    assert isinstance(receipts, list)
    [raw] = receipts
    assert isinstance(raw, dict)
    receipt = receipt_from_json(raw)

    assert receipt.refunds
    notes = {refund.note_from_issuer for refund in receipt.refunds}
    if automation_id in BUYER_DATA_AUTOMATIONS:
        assert notes == {FAKE_NOTE}
    else:
        assert notes == {None}
        assert FAKE_NOTE not in json.dumps(output)
