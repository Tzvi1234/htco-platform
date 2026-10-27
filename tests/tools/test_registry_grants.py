"""AUD-06 (#147 part B): the Etsy reads that carry buyer data or tracking codes are granted to
one automation each, and the registry check refuses the grant to anything else."""

import shutil
import tomllib
from pathlib import Path

import pytest

from htco_application.workforce.files import read_action_catalog, read_registry
from htco_application.workforce.registry import Registry
from tools.registry import ACTION_SCHEMAS, RESTRICTED_GRANTS, main, unauthorized_grants

ROOT = Path(__file__).resolve().parents[2] / "registry"
DOCUMENTS = read_registry(ROOT)
REGISTRY = DOCUMENTS.registry()
CATALOG = read_action_catalog(ROOT, ACTION_SCHEMAS, REGISTRY)

ORDER_HISTORY = "etsy.order_history.read"
SHIPMENTS = "etsy.shipments.read"


def _with_automation_grant(automation_id: str, action: str) -> Registry:
    automations = [
        {**document, "allowed_actions": [*document.get("allowed_actions", []), action]}
        if document["id"] == automation_id
        else document
        for document in DOCUMENTS.automations
    ]
    return Registry.from_documents(
        employees=DOCUMENTS.employees,
        tools=DOCUMENTS.tools,
        automations=automations,
        advisors=DOCUMENTS.advisors,
    )


def _with_employee_grant(employee_id: str, tool: str) -> Registry:
    employees = [
        {**document, "allowed_tools": [*document["allowed_tools"], tool]}
        if document["id"] == employee_id
        else document
        for document in DOCUMENTS.employees
    ]
    return Registry.from_documents(
        employees=employees,
        tools=DOCUMENTS.tools,
        automations=DOCUMENTS.automations,
        advisors=DOCUMENTS.advisors,
    )


def test_restricted_grants__are_the_design_of_aud_06() -> None:
    assert dict(RESTRICTED_GRANTS) == {
        ORDER_HISTORY: frozenset({"etsy-order-intake"}),
        SHIPMENTS: frozenset({"tracking-sync"}),
    }


def test_the_committed_registry__grants_each_restricted_read_to_its_automation_only() -> None:
    assert unauthorized_grants(REGISTRY) == []
    holders = {
        action: {a.id for a in REGISTRY.automations.values() if action in a.allowed_actions}
        for action in RESTRICTED_GRANTS
    }
    assert holders == {ORDER_HISTORY: {"etsy-order-intake"}, SHIPMENTS: {"tracking-sync"}}


def test_no_employee__holds_a_restricted_read() -> None:
    for profile in REGISTRY.employees.values():
        assert not profile.allowed_tools & RESTRICTED_GRANTS.keys(), profile.identity.id


@pytest.mark.parametrize(
    "automation_id", sorted(a for a in REGISTRY.automations if a != "etsy-order-intake")
)
def test_order_history_read__is_refused_to(automation_id: str) -> None:
    problems = unauthorized_grants(_with_automation_grant(automation_id, ORDER_HISTORY))

    assert len(problems) == 1
    assert automation_id in problems[0]
    assert ORDER_HISTORY in problems[0]


@pytest.mark.parametrize(
    "automation_id", sorted(a for a in REGISTRY.automations if a != "tracking-sync")
)
def test_shipments_read__is_refused_to(automation_id: str) -> None:
    problems = unauthorized_grants(_with_automation_grant(automation_id, SHIPMENTS))

    assert len(problems) == 1
    assert automation_id in problems[0]
    assert SHIPMENTS in problems[0]


@pytest.mark.parametrize("action", sorted(RESTRICTED_GRANTS))
def test_a_restricted_read__is_refused_to_an_employee(action: str) -> None:
    problems = unauthorized_grants(_with_employee_grant("eitan", action))

    assert len(problems) == 1
    assert "eitan" in problems[0]
    assert action in problems[0]


def test_main__an_unauthorized_grant__exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "registry"
    shutil.copytree(ROOT, root)
    path = root / "automations" / "order-dedup-verification.toml"
    text = path.read_text(encoding="utf-8")
    old = 'allowed_actions = ["etsy.receipts.read"]'
    assert old in text
    path.write_text(
        text.replace(old, f'allowed_actions = ["etsy.receipts.read", "{ORDER_HISTORY}"]'),
        encoding="utf-8",
    )

    assert main(["--check", "--root", str(root)]) == 1
    error = capsys.readouterr().err
    assert "order-dedup-verification" in error
    assert ORDER_HISTORY in error


# --- The contracts of the three receipts reads -----------------------------------------------


def _actions() -> dict[str, dict]:
    document = tomllib.loads((ROOT / "actions" / "etsy_open_api.toml").read_text("utf-8"))
    return {action["tool"]: action for action in document["actions"]}


@pytest.mark.parametrize(
    ("action", "recipe"),
    [(ORDER_HISTORY, "list_order_history"), (SHIPMENTS, "list_shipments")],
)
def test_a_new_read__is_r1_automation_only_and_disabled_until_its_recipe(
    action: str, recipe: str
) -> None:
    raw = _actions()[action]
    definition = CATALOG.actions[action]

    assert definition.approval_policy.value == "R1"
    assert not definition.tool.mutating
    assert raw["actor_kinds"] == ["automation"]
    assert "user_capability" not in raw  # no user submits it
    [binding] = raw["bindings"]
    assert binding == {
        "channel": "api",
        "recipe": recipe,
        "requires_capability": "receipts_read",
        "breaker_key": "etsy:api",
        "disabled": True,
    }


def test_receipts_read__keeps_its_contract() -> None:
    definition = CATALOG.actions["etsy.receipts.read"]

    assert definition.approval_policy.value == "R1"
    assert definition.bindings[0].recipe == "list_receipts"
    assert "order_history" in definition.input_schema["properties"]


def test_order_history_and_shipments__take_the_same_input_without_a_mode_flag() -> None:
    history = CATALOG.actions[ORDER_HISTORY].input_schema
    shipments = CATALOG.actions[SHIPMENTS].input_schema

    assert {k: v for k, v in history.items() if k not in ("$id", "title")} == {
        k: v for k, v in shipments.items() if k not in ("$id", "title")
    }
    assert set(history["properties"]) == {
        "receipt_id",
        "min_last_modified",
        "limit",
        "offset",
        "was_paid",
        "was_shipped",
    }
    assert history["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    }  # the default of 100 is the typed client's: the schema subset has no `default`
    assert history["additionalProperties"] is False


def test_order_history_output__keeps_receipts_generic() -> None:
    output = CATALOG.actions[ORDER_HISTORY].output_schema

    assert output["properties"]["receipts"]["items"] == {"type": "object"}
    assert set(output["properties"]) == {"receipts", "count"}


def test_shipments_output__carries_tracking_codes_and_no_buyer_field() -> None:
    output = CATALOG.actions[SHIPMENTS].output_schema
    receipt = output["properties"]["shipments"]["items"]
    shipment = receipt["properties"]["shipments"]["items"]

    assert set(output["properties"]) == {"shipments", "count"}
    assert set(receipt["properties"]) == {"receipt_id", "shipments"}
    assert set(shipment["properties"]) == {
        "tracking_code",
        "carrier_name",
        "shipment_notification_timestamp",
    }
    assert output["additionalProperties"] is False
    assert receipt["additionalProperties"] is False
    assert shipment["additionalProperties"] is False
