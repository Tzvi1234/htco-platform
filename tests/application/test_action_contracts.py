"""Action contracts (slice 5.12): the action state machine, the JSON Schema checker and the
catalog built from `registry/actions/` and `packages/contracts/actions/`."""

from typing import Any

import pytest

from htco_application.execution.contracts import ActionCatalog, ActionCatalogError
from htco_application.execution.gateway import Channel
from htco_application.execution.schema import check_schema, validate
from htco_application.workforce.files import read_registry
from htco_domain.actions import ActionState, ApprovalPolicy, action_path
from htco_domain.state_machine import IllegalTransition
from tests.execution_fakes import REGISTRY, real_catalog

S = ActionState


@pytest.mark.parametrize(
    "path",
    [
        (S.RECEIVED, S.AUTHORIZED, S.ROUTED, S.RUNNING, S.SUCCEEDED),
        (S.RECEIVED, S.AUTHORIZED, S.ROUTED, S.RUNNING, S.VERIFYING, S.SUCCEEDED),
        (S.RECEIVED, S.AUTHORIZED, S.WAITING_APPROVAL, S.AUTHORIZED, S.ROUTED),
        (S.RECEIVED, S.DENIED),
        (S.WAITING_APPROVAL, S.REJECTED),
        (S.RUNNING, S.HANDED_TO_HUMAN, S.RUNNING),
        (S.RUNNING, S.FAILED, S.AUTHORIZED, S.ROUTED),
        (S.FAILED, S.DEAD_LETTERED),
    ],
)
def test_action_path__allowed(path: tuple[ActionState, ...]) -> None:
    action_path(*path)


@pytest.mark.parametrize(
    "path",
    [
        (S.RECEIVED, S.RUNNING),
        (S.SUCCEEDED, S.RUNNING),
        (S.DENIED, S.AUTHORIZED),
        (S.ROUTED, S.SUCCEEDED),
    ],
)
def test_action_path__anything_else__rejected(path: tuple[ActionState, ...]) -> None:
    with pytest.raises(IllegalTransition):
        action_path(*path)


def test_approval_policy__writes_and_audits() -> None:
    assert [p.is_write for p in ApprovalPolicy] == [False, False, True, True, True]
    assert not ApprovalPolicy.R0.audited
    assert ApprovalPolicy.R1.audited


SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["listing_id"],
    "properties": {
        "listing_id": {"type": "integer", "minimum": 1},
        "sku": {"type": "string", "maxLength": 4, "pattern": "^[A-Z]+$"},
        "state": {"enum": ["active", "draft"]},
        "tags": {"type": "array", "maxItems": 2, "items": {"type": "string", "minLength": 1}},
        "price": {"type": "number", "maximum": 10},
        "flag": {"type": "boolean"},
        "note": {"type": "null"},
    },
    "additionalProperties": False,
}


def test_validate__a_valid_object__no_problem() -> None:
    value = {"listing_id": 1, "sku": "AB", "state": "draft", "tags": ["a"], "price": 9.5}

    assert validate(value, SCHEMA) == []


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({}, "$.listing_id: required"),
        ({"listing_id": 0}, "below the minimum"),
        ({"listing_id": True}, "expected integer"),
        ({"listing_id": 1, "sku": "ABCDE"}, "too long"),
        ({"listing_id": 1, "sku": "ab"}, "pattern"),
        ({"listing_id": 1, "state": "deleted"}, "allowed values"),
        ({"listing_id": 1, "tags": ["a", "b", "c"]}, "too many items"),
        ({"listing_id": 1, "tags": [""]}, "too short"),
        ({"listing_id": 1, "price": 11}, "above the maximum"),
        ({"listing_id": 1, "other": 1}, "not allowed"),
        ({"listing_id": 1, "flag": "yes"}, "expected boolean"),
        ({"listing_id": 1, "note": 1}, "expected null"),
        ([], "expected object"),
    ],
)
def test_validate__each_violation__named_without_the_value(value: Any, problem: str) -> None:
    problems = validate(value, SCHEMA)

    assert any(problem in p for p in problems), problems
    assert all("ABCDE" not in p for p in problems)


@pytest.mark.parametrize(
    "schema",
    [{"type": "object", "oneOf": []}, {"type": "date"}, {"properties": {"a": {"format": "x"}}}],
)
def test_check_schema__a_keyword_the_checker_does_not_apply__refused(schema: Any) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        check_schema(schema)


def test_catalog__the_registrys_actions_load() -> None:
    catalog = real_catalog()

    receipts = catalog.get("etsy.receipts.read", 1)
    assert receipts is not None
    assert receipts.approval_policy is ApprovalPolicy.R1
    assert [b.channel for b in receipts.bindings] == [Channel.API]
    assert catalog.get("etsy.receipts.read", 2) is None
    write = catalog.get("etsy.inventory.update", 1)
    assert write is not None
    assert write.approval_policy.is_write


def _document(**changes: Any) -> dict[str, Any]:
    return {
        "tool": "etsy.shop.read",
        "approval_policy": "R0",
        "verify": "none",
        "timeout_seconds": 30,
        "input_schema": "in.json",
        "output_schema": "out.json",
        "bindings": [{"channel": "api"}],
    } | changes


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"tool": "etsy.nope.read"}, "no such tool"),
        ({"approval_policy": "R1"}, "differs from the tool's description"),
        ({"tool": "etsy.inventory.update", "approval_policy": "W2"}, None),
        ({"bindings": []}, "at least one channel binding"),
        ({"input_schema": "missing.json"}, "missing.json"),
        ({"bindings": [{"channel": "teleport"}]}, "teleport"),
    ],
)
def test_catalog__a_bad_contract__refused_with_its_problem(
    changes: dict[str, Any], problem: str | None
) -> None:
    tools = read_registry(REGISTRY).registry().tools
    schemas = {"in.json": {"type": "object"}, "out.json": {"type": "object"}}

    if problem is None:
        catalog = ActionCatalog.from_documents([_document(**changes)], tools=tools, schemas=schemas)
        assert len(catalog.actions) == 1
        return
    with pytest.raises(ActionCatalogError, match=problem):
        ActionCatalog.from_documents([_document(**changes)], tools=tools, schemas=schemas)


def test_catalog__a_duplicate_action__refused() -> None:
    tools = read_registry(REGISTRY).registry().tools
    schemas = {"in.json": {"type": "object"}, "out.json": {"type": "object"}}

    with pytest.raises(ActionCatalogError, match="duplicate"):
        ActionCatalog.from_documents([_document(), _document()], tools=tools, schemas=schemas)
