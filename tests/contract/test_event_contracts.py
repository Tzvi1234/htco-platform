"""Event contracts (AUD-01, AC-2 and AC-3): every event type built in the source has a JSON
Schema in `packages/contracts/events/`, every schema has a catalog row, and every catalog row
that links a schema points at one that exists.

The producers' tests validate the events they build against these schemas with
`tests.event_contracts.assert_event_contract`.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from htco_application.execution.schema import check_schema
from htco_application.kernel.events import EVENT_TYPE
from tests.event_contracts import EVENTS_DIR, assert_event_contract, load_event_schema

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
