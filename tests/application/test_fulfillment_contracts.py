"""The JSON Schema contracts of the Phase 6 automations (packages/contracts/automations/): valid
for the gateway's checker, and the payloads the triggers send and the handlers return match."""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from htco_application.execution.schema import check_schema, validate

CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "automations"


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    return loaded


@pytest.mark.parametrize("name", sorted(p.name for p in CONTRACTS.glob("*.json")))
def test_every_contract_uses_only_the_checked_keywords(name: str) -> None:
    check_schema(_schema(name))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("tracking-sync.input.json", {"receipt_id": 4000000001, "event_type": "order.shipped"}),
        ("tracking-sync.input.json", {"mode": "schedule"}),
        ("tracking-sync.output.json", {"receipts": 1, "recorded": 2, "via": "webhook"}),
        ("tracking-sync.output.json", {"skipped": "not_connected"}),
        ("incident-detection-escalation.input.json", {"mode": "inspect"}),
        (
            "incident-detection-escalation.input.json",
            {
                "drift": {
                    "shop_id": str(uuid.uuid4()),
                    "receipt_id": 7,
                    "kind": "status",
                    "detected_at": "2026-10-25T09:00:00+00:00",
                    "drift_id": str(uuid.uuid4()),
                }
            },
        ),
        (
            "incident-detection-escalation.output.json",
            {"checked": 3, "opened": 1, "resolved": 0, "escalated": 0},
        ),
        (
            "incident-detection-escalation.output.json",
            {"incident_id": str(uuid.uuid4()), "opened": 1},
        ),
    ],
)
def test_the_payloads_match_their_contract(name: str, value: dict[str, Any]) -> None:
    assert validate(value, _schema(name)) == []


def test_buyer_data_does_not_fit_the_inputs() -> None:
    assert validate({"receipt_id": 1, "buyer_name": "x"}, _schema("tracking-sync.input.json"))
    assert validate({"mode": "everything"}, _schema("incident-detection-escalation.input.json"))
