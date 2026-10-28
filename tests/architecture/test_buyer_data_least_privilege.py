"""Least privilege for buyer data (ADR-0018, ER-SEC-02; audit AUD-06 in #134 and #147).

Receipts reach automations through three actions, each with its own grant:

- `etsy.order_history.read`: whole receipts as the order history stores them. Granted to Etsy
  Order Intake alone (`RESTRICTED_GRANTS`), the only automation that receives the buyer's name,
  shipping address and the seller's refund note; still shaped by `for_automation`, so any other
  automation would get them stripped.
- `etsy.shipments.read`: each receipt's shipments with their tracking codes, and nothing of the
  buyer. Granted to Tracking Sync alone.
- `etsy.receipts.read`, its order history form: the same receipts without the buyer's data and
  without tracking codes, for every automation (Order Deduplication & Verification), Intake's
  name included.

Invented values only."""

import json
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from htco_application.etsy_reads import ORDER_HISTORY_READ, RECEIPTS_READ, SHIPMENTS_READ
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.contracts import ChannelBinding
from htco_application.execution.gateway import ActionRef, ActionRequest, Channel
from htco_application.kernel import JsonValue
from htco_application.marketplace import MarketplacePermissionError, OrderReceipt
from htco_application.order_receipts import (
    BUYER_DATA_AUTOMATIONS,
    TRACKING_CODE_AUTOMATIONS,
    receipt_from_json,
    shipments_from_json,
)
from htco_application.workforce.registry import RESTRICTED_GRANTS
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
RECEIPT_ACTIONS = (RECEIPTS_READ.name, ORDER_HISTORY_READ.name, SHIPMENTS_READ.name)


def _grants() -> dict[str, set[str]]:
    granted: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "registry/automations").glob("*.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        for action in set(document.get("allowed_actions", [])) & set(RECEIPT_ACTIONS):
            granted.setdefault(action, set()).add(str(document["id"]))
    return granted


GRANTS = _grants()
CALLERS = sorted(set().union(*GRANTS.values()))
# Any automation granted an action later must not get buyer data by default either.
CANDIDATES = [*CALLERS, "a-future-automation"]


def test_each_receipt_action__granted_to_its_automations_only() -> None:
    assert frozenset({"etsy-order-intake"}) == BUYER_DATA_AUTOMATIONS
    # Tracking codes can locate the parcel and so the buyer (ADR-0018): Intake stores them and
    # Tracking Sync records them; nothing else needs them.
    assert frozenset({"etsy-order-intake", "tracking-sync"}) == TRACKING_CODE_AUTOMATIONS
    assert GRANTS[ORDER_HISTORY_READ.name] == {"etsy-order-intake"}
    assert GRANTS[SHIPMENTS_READ.name] == {"tracking-sync"}
    assert GRANTS[RECEIPTS_READ.name] == {"order-dedup-verification"}
    for action, allowed in RESTRICTED_GRANTS.items():
        assert GRANTS[action] <= allowed


def _receipt() -> OrderReceipt:
    receipt = refunded(shipped(paid_receipt(RECEIPT), minutes=30), minutes=60)
    return replace(receipt, shipments=shipped(receipt, minutes=30).shipments)


async def _read(
    action: ActionRef, recipe: str, automation_id: str, values: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    channel = EtsyApiChannel(FakeMarketplace(order_receipts=(_receipt(),)), EtsyShops())
    outcome = await channel.execute(
        ActionRequest(
            action=action,
            actor=ActorRef(ActorKind.AUTOMATION, automation_id),
            organization_id=ORG,
            shop_id=SHOP,
            idempotency_key=f"{automation_id}:{uuid.uuid4()}",
            correlation_id="c",
            input=values,
        ),
        ChannelBinding(Channel.API, recipe=recipe),
    )
    assert outcome.output is not None
    return dict(outcome.output)


def _one(output: dict[str, JsonValue], key: str = "receipts") -> dict[str, JsonValue]:
    items = output[key]
    assert isinstance(items, list)
    [raw] = items
    assert isinstance(raw, dict)
    return raw


VALUES: list[dict[str, JsonValue]] = [{"receipt_id": RECEIPT}, {"limit": 100, "offset": 0}]


@pytest.mark.parametrize("automation_id", CANDIDATES)
@pytest.mark.parametrize("values", VALUES)
async def test_order_history_read__buyer_data_and_the_refund_note_reach_intake_only(
    automation_id: str, values: dict[str, JsonValue]
) -> None:
    output = await _read(ORDER_HISTORY_READ, "list_order_history", automation_id, values)
    raw = _one(output)
    text = json.dumps(output)
    receipt = receipt_from_json(raw)

    if automation_id in BUYER_DATA_AUTOMATIONS:
        assert receipt.buyer_name == FAKE_NAME
        assert receipt.shipping_address is not None
        assert {refund.note_from_issuer for refund in receipt.refunds} == {FAKE_NOTE}
    else:
        assert raw["buyer_name"] is None
        assert raw["shipping_address"] is None
        for value in (FAKE_NAME, FAKE_LINE, FAKE_NOTE):
            assert value not in text
    codes = {shipment.tracking_code for shipment in receipt.shipments}
    assert codes == ({FAKE_TRACKING} if automation_id in TRACKING_CODE_AUTOMATIONS else {""})
    # The order facts stay: items, totals, shipments and refunds.
    assert receipt.receipt_id == RECEIPT
    assert receipt.lines
    assert receipt.shipments
    assert receipt.refunds


@pytest.mark.parametrize("automation_id", CANDIDATES)
@pytest.mark.parametrize("values", VALUES)
async def test_receipts_read__the_order_history_form_never_has_buyer_data_or_codes(
    automation_id: str, values: dict[str, JsonValue]
) -> None:
    """Order Deduplication & Verification compares totals and items, never shipments (quill on
    #135): it gets the shipments without their codes, and no buyer data, under any name."""
    output = await _read(
        RECEIPTS_READ, "list_receipts", automation_id, {"order_history": True, **values}
    )
    raw = _one(output)
    text = json.dumps(output)
    receipt = receipt_from_json(raw)

    assert raw["buyer_name"] is None
    assert raw["shipping_address"] is None
    for value in (FAKE_NAME, FAKE_LINE, FAKE_NOTE, FAKE_TRACKING):
        assert value not in text
    assert receipt.lines
    assert {shipment.tracking_code for shipment in receipt.shipments} == {""}
    assert {refund.note_from_issuer for refund in receipt.refunds} == {None}


@pytest.mark.parametrize("values", VALUES)
async def test_shipments_read__tracking_codes_and_nothing_of_the_buyer(
    values: dict[str, JsonValue],
) -> None:
    output = await _read(SHIPMENTS_READ, "list_shipments", "tracking-sync", values)
    raw = _one(output, "shipments")
    text = json.dumps(output)

    assert set(raw) == {"receipt_id", "updated_timestamp", "shipments"}
    read = shipments_from_json(raw)
    assert read.receipt_id == RECEIPT
    assert {shipment.tracking_code for shipment in read.shipments} == {FAKE_TRACKING}
    for value in (FAKE_NAME, FAKE_LINE, FAKE_NOTE):
        assert value not in text


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
