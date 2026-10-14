"""Least-privilege tool access, decided by the backend (Scenario H, FR-AGT-04)."""

from typing import Any

import pytest

from htco_application.workforce.registry import Registry
from htco_application.workforce.tool_access import ToolVerdict, decide_tool_access
from tests.application.workforce_fixtures import (
    advisor_document,
    automation_document,
    employee_document,
    tool_document,
)

TOOLS = [
    tool_document("catalog.read"),
    tool_document("orders.read", owner="eitan"),
    tool_document(
        "catalog.publish_draft",
        side_effect="external",
        risk="high",
        audit_required=True,
    ),
    tool_document("finance.refund", side_effect="financial", risk="critical", audit_required=True),
    tool_document("catalog.import", readiness="draft"),
]


def registry(**shon_changes: Any) -> Registry:
    shon = employee_document(
        allowed_tools=["catalog.read", "catalog.publish_draft", "finance.refund", "catalog.import"],
        **shon_changes,
    )
    return Registry.from_documents(
        employees=[
            shon,
            employee_document("eitan", allowed_tools=["orders.read"]),
            employee_document("dana", readiness="draft"),
        ],
        tools=TOOLS,
        automations=[automation_document()],
        advisors=[advisor_document()],
    )


def test_granted_read_only_tool__is_allowed() -> None:
    assert decide_tool_access(registry(), "shon", "catalog.read").verdict is ToolVerdict.ALLOW


@pytest.mark.ac("SCN-H")
def test_tool_outside_the_employees_grants__is_denied_even_if_requested() -> None:
    decision = decide_tool_access(registry(), "shon", "orders.read")

    assert decision.verdict is ToolVerdict.DENY
    assert "not granted" in decision.reason


@pytest.mark.ac("SCN-H")
def test_unknown_tool_or_employee__is_denied() -> None:
    assert decide_tool_access(registry(), "shon", "db.drop_everything").verdict is ToolVerdict.DENY
    assert decide_tool_access(registry(), "mallory", "catalog.read").verdict is ToolVerdict.DENY


@pytest.mark.ac("SCN-H")
def test_employee_not_yet_production_ready__is_denied_every_tool() -> None:
    decision = decide_tool_access(registry(), "dana", "catalog.read")

    assert decision.verdict is ToolVerdict.DENY
    assert "not active" in decision.reason


def test_tool_not_yet_ready__is_denied() -> None:
    decision = decide_tool_access(registry(), "shon", "catalog.import")

    assert decision.verdict is ToolVerdict.DENY
    assert "tool" in decision.reason


@pytest.mark.parametrize("autonomy", ["L0", "L1", "L2"])
def test_external_tool_below_l3__needs_approval(autonomy: str) -> None:
    decision = decide_tool_access(registry(autonomy=autonomy), "shon", "catalog.publish_draft")

    assert decision.verdict is ToolVerdict.REQUIRE_APPROVAL


def test_external_tool_at_l3__is_allowed_within_policy() -> None:
    decision = decide_tool_access(registry(autonomy="L3"), "shon", "catalog.publish_draft")

    assert decision.verdict is ToolVerdict.ALLOW


def test_critical_tool__always_needs_approval_even_at_l3() -> None:
    decision = decide_tool_access(registry(autonomy="L3"), "shon", "finance.refund")

    assert decision.verdict is ToolVerdict.REQUIRE_APPROVAL
