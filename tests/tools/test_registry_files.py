"""The office as committed in `registry/`: the organization of ADR-0012, validated."""

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from htco_application.workforce.registry import Registry
from htco_domain.automation import AUTOMATION_SPEC_SECTIONS, TriggerKind
from htco_domain.routing import ModelTier
from htco_domain.tooling import SideEffect
from htco_domain.workforce import Department, Readiness, check_specification
from tools.registry import load_registry, main

REGISTRY = load_registry(Path(__file__).resolve().parents[2] / "registry")

ORGANIZATION = {
    Department.EXECUTIVE_OFFICE: {"maya", "noa", "omer", "lia"},
    Department.PRODUCT_CATALOG: {"shon", "noam", "nika", "daniel"},
    Department.PROCUREMENT_FINANCE: {"tom", "roni"},
    Department.ORDER_ASSURANCE: {"eitan"},
    Department.MARKETING: {"gal"},
    Department.CUSTOMER_SERVICE: {"dana"},
    Department.ENGINEERING: {"alex", "amir", "iris"},
}
AUTOMATIONS = {
    "etsy-order-intake",
    "order-dedup-verification",
    "product-sales-counters",
    "catalog-draft-publishing",
    "supplier-quote-followup",
    "tracking-sync",
    "finance-ledger-ingestion",
    "inventory-price-monitoring",
    "order-financial-reconciliation",
    "incident-detection-escalation",
    "email-intake-routing",
    "scheduled-reporting",
    "agent-task-scheduling-recovery",
    "quality-review-scheduling",
    "optimization-review-scheduling",
}


def test_workforce__is_sixteen_employees_in_seven_departments() -> None:
    assert len(REGISTRY.employees) == 16
    assert {
        department: {p.identity.id for p in REGISTRY.employees_in(department)}
        for department in Department
    } == ORGANIZATION


def test_jev__is_a_shared_advisor_not_an_employee() -> None:
    assert set(REGISTRY.advisors) == {"jev"}
    assert "jev" not in REGISTRY.employees


def test_automations__are_the_fifteen_initial_workflows() -> None:
    assert set(REGISTRY.automations) == AUTOMATIONS


def test_automations__are_deterministic() -> None:
    """Automations consume no model tokens; a reasoning step must be named and justified."""
    assert all(a.ai_steps == () for a in REGISTRY.automations.values())


def test_every_automation__has_its_specification_and_an_owner() -> None:
    owners = {a.business_owner for a in REGISTRY.automations.values()}

    assert owners <= set(REGISTRY.employees) | {"owner"}


def test_external_automations__need_approval() -> None:
    for automation in REGISTRY.automations.values():
        if automation.side_effect is SideEffect.EXTERNAL:
            assert automation.approval_required, automation.id


def test_granted_automations__are_callable_as_agent_tools() -> None:
    for profile in REGISTRY.employees.values():
        for automation_id in profile.allowed_automations:
            callable_from = REGISTRY.automations[automation_id].callable_from
            assert TriggerKind.AGENT_TOOL in callable_from, (profile.identity.id, automation_id)


# The automations the owner approved for activation (ADR-0012 rule 2): the three order
# automations of slice 5.11 (operations.md, activation checklist step 2). Every other one stays
# draft until its own approval.
OWNER_ACTIVATED = {"etsy-order-intake", "order-dedup-verification", "product-sales-counters"}


def test_nothing__is_production_ready_by_configuration_alone() -> None:
    assert not any(p.is_production_ready() for p in REGISTRY.employees.values())
    assert all(p.incomplete_sections() for p in REGISTRY.employees.values())
    assert all(t.readiness.value == "draft" for t in REGISTRY.tools.values())
    assert {
        a.id: a.readiness.value
        for a in REGISTRY.automations.values()
        if a.readiness.value != "draft"
    } == dict.fromkeys(OWNER_ACTIVATED, "ready")


# Every section defined: the order automations (AC-5.11.1) and the fulfillment ones (AC-6.7.1).
# Readiness is draft until the owner approves the activation, ready once he has.
FULLY_SPECIFIED = {
    "etsy-order-intake",
    "order-dedup-verification",
    "product-sales-counters",
    "tracking-sync",
    "incident-detection-escalation",
}


@pytest.mark.parametrize("automation_id", sorted(FULLY_SPECIFIED))
def test_a_fully_specified_automation__is_ready_only_with_the_owners_approval(
    automation_id: str,
) -> None:
    automation = REGISTRY.automations[automation_id]

    assert check_specification(automation.specification, AUTOMATION_SPEC_SECTIONS) == ()
    expected = Readiness.READY if automation_id in OWNER_ACTIVATED else Readiness.DRAFT
    assert automation.readiness is expected  # the owner's switch, not this file's
    replace(automation, readiness=Readiness.READY)  # the loader's check would accept it


def test_every_employee__has_its_own_routing_policy() -> None:
    versions = Counter(p.routing.version for p in REGISTRY.employees.values())

    assert all(count == 1 for count in versions.values())


def test_no_employee__defaults_to_the_most_expensive_tier() -> None:
    assert not any(
        p.routing.default_tier is ModelTier.CRITICAL for p in REGISTRY.employees.values()
    )


@pytest.mark.parametrize(
    ("employee", "phase"),
    [
        ("eitan", 9),
        ("noa", 10),
        ("daniel", 11),
        ("shon", 12),
        ("tom", 13),
        ("dana", 14),
        ("maya", 15),
        ("omer", 18),
        ("alex", 19),
        ("gal", 21),
    ],
)
def test_activation_phases__follow_the_plan(employee: str, phase: int) -> None:
    assert REGISTRY.employees[employee].activation_phase == phase


def test_main__check__prints_a_summary(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check"]) == 0
    assert "16 employees" in capsys.readouterr().out


def test_main__invalid_registry__exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "employees").mkdir()
    (tmp_path / "employees" / "ghost.toml").write_text('id = "ghost"\n', encoding="utf-8")

    assert main(["--check", "--root", str(tmp_path)]) == 1
    assert "ghost" in capsys.readouterr().err


def test_load_registry__returns_a_registry() -> None:
    assert isinstance(REGISTRY, Registry)
