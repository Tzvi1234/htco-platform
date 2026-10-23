"""The JSON Schema contracts of the three order automations (packages/contracts/automations/,
slice 5.11): the payloads their triggers send and the outputs their handlers return match, and
no buyer field is allowed."""

import json
from pathlib import Path
from typing import Any

import pytest

from htco_application.execution.schema import check_schema, validate

CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "automations"
ORDER_AUTOMATIONS = ("etsy-order-intake", "order-dedup-verification", "product-sales-counters")


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    return loaded


@pytest.mark.parametrize("automation", ORDER_AUTOMATIONS)
@pytest.mark.parametrize("part", ["input", "output"])
def test_every_order_automation__has_both_contracts(automation: str, part: str) -> None:
    check_schema(_schema(f"{automation}.{part}.json"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("etsy-order-intake.input.json", {"receipt_id": 4000000001, "event_type": "order.paid"}),
        ("etsy-order-intake.input.json", {"receipt_id": 4000000001, "event_type": "resync"}),
        ("etsy-order-intake.input.json", {"mode": "sweep", "reason": "sweep"}),
        ("etsy-order-intake.output.json", {"receipt_id": 1, "written": True, "state": "paid"}),
        ("etsy-order-intake.output.json", {"receipt_id": 1, "written": False, "state": None}),
        (
            "etsy-order-intake.output.json",
            {
                "mode": "sweep",
                "receipts_read": 3,
                "drifts": 0,
                "etsy_receipts": 9,
                "orders_stored": 9,
            },
        ),
        ("etsy-order-intake.output.json", {"skipped": "not_connected"}),
        ("order-dedup-verification.input.json", {"receipt_id": 7}),
        (
            "order-dedup-verification.output.json",
            {"receipt_id": 7, "differences": ["totals"], "recorded": 1},
        ),
        (
            "product-sales-counters.input.json",
            {"receipt_id": 7, "event_type": "orders.cancelled.v1"},
        ),
        (
            "product-sales-counters.output.json",
            {"receipt_id": 7, "units": {"11": -2}, "orders": {"11": -1}},
        ),
        ("product-sales-counters.output.json", {"receipt_id": 7, "skipped": "not_stored"}),
    ],
)
def test_contracts__accept_what_the_triggers_and_handlers_use(
    name: str, value: dict[str, Any]
) -> None:
    assert validate(value, _schema(name)) == []


@pytest.mark.parametrize("automation", ORDER_AUTOMATIONS)
def test_contracts__refuse_buyer_fields(automation: str) -> None:
    assert validate({"receipt_id": 1, "buyer_name": "x"}, _schema(f"{automation}.input.json"))
    assert validate({"receipt_id": 1, "buyer_name": "x"}, _schema(f"{automation}.output.json"))
