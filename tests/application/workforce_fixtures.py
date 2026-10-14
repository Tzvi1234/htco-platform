"""Small, valid registry documents for application tests (the real ones live in `registry/`)."""

from copy import deepcopy
from typing import Any

from htco_domain.automation import AUTOMATION_SPEC_SECTIONS
from htco_domain.workforce import EMPLOYEE_SPEC_SECTIONS

EVALUATION = {
    "suite": "core",
    "version": "1",
    "passed": True,
    "evaluated_at": "2026-10-14T12:00:00Z",
}


def _sections(names: tuple[str, ...], status: str) -> dict[str, dict[str, str]]:
    return {name: {"status": status, "text": f"{name}."} for name in names}


def employee_document(employee_id: str = "shon", **changes: Any) -> dict[str, Any]:
    """An active employee by default; pass readiness="draft" for one still being designed."""
    document: dict[str, Any] = {
        "id": employee_id,
        "name": employee_id.capitalize(),
        "title": "Product Intelligence",
        "department": "product_research_content_catalog",
        "version": 1,
        "autonomy": "L1",
        "readiness": "active",
        "activation_phase": 12,
        "allowed_tools": ["catalog.read"],
        "allowed_automations": [],
        "routing": {
            "version": f"{employee_id}-routing-v1",
            "allowed_providers": ["anthropic"],
            "required_capabilities": ["structured_output"],
            "default_tier": "economy",
            "max_tier": "advanced",
            "max_cost_per_task": "0.40",
            "max_data_class": "internal",
            "fallback_tiers": ["balanced"],
            "decision_advisor": "when_rules_inconclusive",
        },
        "evaluation": dict(EVALUATION),
        "specification": _sections(EMPLOYEE_SPEC_SECTIONS, "defined"),
    }
    if changes.get("readiness") == "draft":
        document["specification"] = _sections(EMPLOYEE_SPEC_SECTIONS, "draft")
        document.pop("evaluation")
    document.update(changes)
    return deepcopy(document)


def tool_document(name: str = "catalog.read", **changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": name,
        "version": 1,
        "family": "commerce_catalog",
        "owner": "shon",
        "side_effect": "none",
        "risk": "low",
        "data_scope": "internal",
        "audit_required": False,
        "readiness": "active",
        "description": "Read the catalog.",
    }
    document.update(changes)
    return document


def automation_document(automation_id: str = "etsy-order-intake", **changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": automation_id,
        "name": "Etsy Order Intake",
        "version": 1,
        "business_owner": "shon",
        "primary_trigger": "event",
        "callable_from": ["event", "schedule", "agent_tool"],
        "retry": {"max_attempts": 3, "initial_backoff_seconds": 5, "max_backoff_seconds": 60},
        "timeout_seconds": 30,
        "side_effect": "internal",
        "approval_required": False,
        "ai_steps": [],
        "readiness": "active",
        "activation_phase": 5,
        "specification": _sections(AUTOMATION_SPEC_SECTIONS, "defined"),
    }
    document.update(changes)
    return document


def advisor_document(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": "jev",
        "name": "Jev",
        "title": "Decision Advisor",
        "providers": ["typesafe", "rules"],
        "readiness": "draft",
        "description": "Shared structured-decision service; not an employee.",
    }
    document.update(changes)
    return document
