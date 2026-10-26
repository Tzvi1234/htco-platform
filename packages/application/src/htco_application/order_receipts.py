"""An order history receipt as JSON (slice 5.4): how `etsy.receipts.read` hands Etsy Order
Intake the whole receipt through the Action Gateway.

The JSON holds what the order history stores (ADR-0018): order facts, items, shipments with their
tracking numbers, refunds with the seller's note, and the buyer's name and shipping address. It is
an R1 output: the gateway returns it to the automation and never stores, logs or audits it, and
only Etsy Order Intake's service identity receives it (the API channel refuses the order history
form to any other actor). Contact data is never in it: the adapter never reads it.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from htco_application.jobs import JsonValue
from htco_application.marketplace import (
    MarketplaceMoney,
    OrderLine,
    OrderReceipt,
    OrderRefund,
    OrderShipment,
    ShippingAddress,
    display_name,
)

# The automations that receive the buyer's name and shipping address in the order history form
# (ADR-0018). Every other caller of the form gets the same receipts without them (AUD-06).
BUYER_DATA_AUTOMATIONS = frozenset({"etsy-order-intake"})
# The automations that receive shipments' tracking codes, which can locate the parcel and so the
# buyer (ADR-0018): Intake stores them, Tracking Sync records them. Others get the shipments
# without them.
TRACKING_CODE_AUTOMATIONS = frozenset({"etsy-order-intake", "tracking-sync"})


def without_buyer_data(receipt: OrderReceipt) -> OrderReceipt:
    """The same receipt without the buyer's name, display name, shipping address and the
    seller's refund notes (shown only to buyer-data roles): the order history form for every
    automation outside `BUYER_DATA_AUTOMATIONS`."""
    return replace(
        receipt,
        buyer_name=None,
        buyer_display_name=None,
        shipping_address=None,
        refunds=tuple(replace(r, note_from_issuer=None) for r in receipt.refunds),
    )


def without_tracking_codes(receipt: OrderReceipt) -> OrderReceipt:
    """The same receipt with each shipment's carrier and time but an empty tracking code: the
    order history form for every automation outside `TRACKING_CODE_AUTOMATIONS`."""
    return replace(
        receipt, shipments=tuple(replace(s, tracking_code="") for s in receipt.shipments)
    )


def for_automation(receipt: OrderReceipt, automation_id: str) -> OrderReceipt:
    """The order history form an automation may receive (ADR-0018, AUD-06): buyer data and
    tracking codes only for the automations listed for them."""
    if automation_id not in BUYER_DATA_AUTOMATIONS:
        receipt = without_buyer_data(receipt)
    if automation_id not in TRACKING_CODE_AUTOMATIONS:
        receipt = without_tracking_codes(receipt)
    return receipt


def _money(money: MarketplaceMoney) -> dict[str, JsonValue]:
    return {"amount": money.amount, "divisor": money.divisor, "currency_code": money.currency_code}


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def receipt_to_json(receipt: OrderReceipt) -> dict[str, JsonValue]:
    address = receipt.shipping_address
    return {
        "receipt_id": receipt.receipt_id,
        "status": receipt.status,
        "is_paid": receipt.is_paid,
        "is_shipped": receipt.is_shipped,
        "created_at": receipt.created_at.isoformat(),
        "updated_at": receipt.updated_at.isoformat(),
        "grand_total": _money(receipt.grand_total),
        "subtotal": _money(receipt.subtotal),
        "shipping_total": _money(receipt.shipping_total),
        "discount_total": _money(receipt.discount_total),
        "country_code": receipt.country_code,
        "lines": [
            {
                "transaction_id": line.transaction_id,
                "listing_id": line.listing_id,
                "title": line.title,
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price": _money(line.unit_price),
                "is_digital": line.is_digital,
                "expected_ship_date": _iso(line.expected_ship_date),
            }
            for line in receipt.lines
        ],
        "shipments": [
            {
                "carrier": shipment.carrier,
                "tracking_code": shipment.tracking_code,
                "notified_at": shipment.notified_at.isoformat(),
            }
            for shipment in receipt.shipments
        ],
        "refunds": [
            {
                "amount": _money(refund.amount),
                "created_at": refund.created_at.isoformat(),
                "reason": refund.reason,
                "status": refund.status,
                "note_from_issuer": refund.note_from_issuer,
            }
            for refund in receipt.refunds
        ],
        "buyer_name": receipt.buyer_name,
        "shipping_address": None
        if address is None
        else {
            "first_line": address.first_line,
            "second_line": address.second_line,
            "city": address.city,
            "state": address.state,
            "postal_code": address.postal_code,
            "country_code": address.country_code,
        },
    }


def _to_money(value: Any) -> MarketplaceMoney:
    return MarketplaceMoney(
        amount=int(value["amount"]),
        divisor=int(value["divisor"]),
        currency_code=str(value["currency_code"]),
    )


def _moment(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def receipt_from_json(value: Mapping[str, Any]) -> OrderReceipt:
    """The receipt back; raises `KeyError`, `TypeError` or `ValueError` on another shape."""
    address = value.get("shipping_address")
    created = _moment(value["created_at"])
    updated = _moment(value["updated_at"])
    if created is None or updated is None:
        raise ValueError("a receipt has its times")
    return OrderReceipt(
        receipt_id=int(value["receipt_id"]),
        status=str(value["status"]),
        is_paid=bool(value["is_paid"]),
        is_shipped=bool(value["is_shipped"]),
        created_at=created,
        updated_at=updated,
        grand_total=_to_money(value["grand_total"]),
        subtotal=_to_money(value["subtotal"]),
        shipping_total=_to_money(value["shipping_total"]),
        discount_total=_to_money(value["discount_total"]),
        country_code=_text(value.get("country_code")),
        lines=tuple(
            OrderLine(
                transaction_id=int(line["transaction_id"]),
                listing_id=None if line.get("listing_id") is None else int(line["listing_id"]),
                title=str(line["title"]),
                sku=_text(line.get("sku")),
                quantity=int(line["quantity"]),
                unit_price=_to_money(line["unit_price"]),
                is_digital=bool(line["is_digital"]),
                expected_ship_date=_moment(line.get("expected_ship_date")),
            )
            for line in value.get("lines", [])
        ),
        shipments=tuple(
            OrderShipment(
                carrier=str(s["carrier"]),
                tracking_code=str(s.get("tracking_code") or ""),
                notified_at=datetime.fromisoformat(str(s["notified_at"])),
            )
            for s in value.get("shipments", [])
        ),
        refunds=tuple(
            OrderRefund(
                amount=_to_money(r["amount"]),
                created_at=datetime.fromisoformat(str(r["created_at"])),
                reason=_text(r.get("reason")),
                status=_text(r.get("status")),
                note_from_issuer=_text(r.get("note_from_issuer")),
            )
            for r in value.get("refunds", [])
        ),
        buyer_name=_text(value.get("buyer_name")),
        buyer_display_name=display_name(_text(value.get("buyer_name"))),
        shipping_address=None
        if not isinstance(address, Mapping)
        else ShippingAddress(
            first_line=_text(address.get("first_line")),
            second_line=_text(address.get("second_line")),
            city=_text(address.get("city")),
            state=_text(address.get("state")),
            postal_code=_text(address.get("postal_code")),
            country_code=_text(address.get("country_code")),
        ),
    )
