"""Event contracts (AUD-01, AC-2 and AC-3): every event type built in the source has a JSON
Schema in `packages/contracts/events/`, every schema has a catalog row, and every catalog row
that links a schema points at one that exists.

The producers' tests validate the events they build against these schemas with
`tests.event_contracts.assert_event_contract`.
"""

import ast
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from htco_application.execution.schema import check_schema, validate
from htco_application.kernel import EVENTS_DESTINATION, JsonValue, OutboxMessage
from htco_application.kernel.events import EVENT_TYPE
from tests.event_contracts import (
    EVENTS_DIR,
    assert_event_contract,
    check_if_event,
    load_event_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
APPLICATION_SOURCE = REPO_ROOT / "packages" / "application" / "src" / "htco_application"
CATALOG = REPO_ROOT / "docs" / "architecture" / "EVENT_CATALOG.md"
_SCHEMA_LINK = re.compile(
    r"\[`([a-z_.0-9]+)`\]\(\.\./\.\./packages/contracts/events/([^)]+)\.json\)"
)


def emitted_event_types() -> set[str]:
    """Every string constant in the application that is shaped like an event type."""
    found: set[str] = set()
    for path in APPLICATION_SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            match node:
                case ast.Constant(value=str() as text) if EVENT_TYPE.match(text):
                    found.add(text)
                case _:
                    pass
    return found


def schema_types() -> set[str]:
    return {path.name.removesuffix(".json") for path in EVENTS_DIR.glob("*.json")}


def catalog_links() -> dict[str, str]:
    """The catalog's event cells that link a schema: event type -> linked schema name."""
    return dict(_SCHEMA_LINK.findall(CATALOG.read_text(encoding="utf-8")))


@pytest.mark.ac("AUD-01.3")
def test_every_event_type_built_in_the_source__has_a_schema() -> None:
    assert emitted_event_types() - schema_types() == set()


@pytest.mark.ac("AUD-01.3")
def test_every_schema__is_an_event_built_in_the_source() -> None:
    assert schema_types() - emitted_event_types() == set()


@pytest.mark.ac("AUD-01.3")
def test_every_schema__has_a_catalog_row_that_links_it() -> None:
    assert schema_types() - set(catalog_links()) == set()


@pytest.mark.ac("AUD-01.3")
def test_every_catalog_link__points_at_its_own_schema() -> None:
    links = catalog_links()

    assert {name: target for name, target in links.items() if name != target} == {}
    assert set(links) - schema_types() == set()


@pytest.mark.ac("AUD-01.2")
@pytest.mark.parametrize("name", sorted(schema_types()))
def test_schema__is_a_2020_12_schema_the_checker_applies(name: str) -> None:
    schema = load_event_schema(name)

    check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["event_type"]["enum"] == [name]
    assert json.loads((EVENTS_DIR / f"{name}.json").read_text(encoding="utf-8")) == schema


def _untyped(node: dict[str, Any], path: str) -> list[str]:
    found: list[str] = []
    for key, child in node.get("properties", {}).items():
        if "type" not in child and not child.get("description", "").startswith("any JSON value"):
            found.append(f"{path}.{key}")
        found += _untyped(child, f"{path}.{key}")
    return found


@pytest.mark.ac("AUD-01.2")
@pytest.mark.parametrize("name", sorted(schema_types()))
def test_schema__every_field_has_a_machine_type(name: str) -> None:
    """Codex (local) on #174: a nullable field is `["<type>", "null"]`, never prose only; only a
    field that is truly any JSON value is untyped, and says so."""
    assert _untyped(load_event_schema(name), "$") == []


@pytest.mark.ac("AUD-01.2")
def test_schema_checker__applies_a_nullable_union() -> None:
    nullable = {"type": ["integer", "null"]}

    check_schema(nullable)
    assert validate(None, nullable) == []
    assert validate(7, nullable) == []
    assert validate("7", nullable) == ["$: expected integer or null"]
    assert validate(True, nullable) == ["$: expected integer or null"]  # a bool is no integer
    broken: list[dict[str, Any]] = [{"type": ["integer", "date"]}, {"type": []}, {"type": "date"}]
    for schema in broken:
        with pytest.raises(ValueError, match="type"):
            check_schema(schema)


@pytest.mark.ac("AUD-01.2")
def test_incidents_opened__a_receipt_of_another_type__breaks_the_contract() -> None:
    receipt = load_event_schema("incidents.opened.v1")["properties"]["payload"]["properties"][
        "receipt_id"
    ]

    assert validate(4000000001, receipt) == []
    assert validate(None, receipt) == []
    assert validate("4000000001", receipt) != []
    assert validate({"receipt": 1}, receipt) != []


FLAT_ACTIONS = (
    "actions.executed.v1",
    "actions.approval_required.v1",
    "actions.handed_to_human.v1",
    "actions.deferred.v1",
)


def _flat_action(event_type: str, **changes: JsonValue) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "event_type": event_type, "action_execution_id": str(uuid.uuid7()),
        "action": "etsy.inventory.update", "version": 1, "outcome": "succeeded",
        "error_code": None, "actor": "automation:etsy-order-intake", "causation_id": None,
    }  # fmt: skip
    payload.update(changes)
    return OutboxMessage(uuid.uuid7(), EVENTS_DESTINATION, f"{event_type}:1", payload)


@pytest.mark.ac("AUD-01.3")
@pytest.mark.parametrize("event_type", FLAT_ACTIONS)
def test_actions__the_temporary_flat_schema_checks_what_the_gateway_emits(event_type: str) -> None:
    """Codex (local) on #174: the gateway's `actions.*` stay flat until #205; their temporary
    schemas describe that flat shape, and the test outboxes check flat messages too."""
    schema = load_event_schema(event_type)

    assert "remove after migration" in schema["description"]
    assert "#205" in schema["description"]
    check_if_event(_flat_action(event_type))
    for broken in (
        _flat_action(event_type, buyer_note="Dana says hi"),
        _flat_action(event_type, version="1"),
        _flat_action(event_type, error_code=7),
    ):
        with pytest.raises(AssertionError, match="breaks its contract"):
            check_if_event(broken)


@pytest.mark.ac("AUD-01.3")
def test_assert_event_contract__a_dropped_field__fails() -> None:
    from tests.event_contracts import sample_envelope  # noqa: PLC0415 - helper for this test

    envelope = sample_envelope(
        "orders.ingested.v1",
        {
            "shop_id": "s",
            "receipt_id": 1,
            "state": "paid",
            "etsy_updated_at": "t",
            "source": "webhook",
        },
    )
    assert_event_contract(envelope)
    payload = envelope["payload"]
    assert isinstance(payload, dict)
    del payload["source"]

    with pytest.raises(AssertionError, match="source"):
        assert_event_contract(envelope)
