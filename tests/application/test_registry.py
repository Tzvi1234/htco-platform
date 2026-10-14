"""The workforce registry: employees, tools, automations and advisors as reviewed data."""

from decimal import Decimal
from typing import Any

import pytest

from htco_application.workforce.registry import Registry, RegistryError
from htco_domain.routing import ModelTier
from htco_domain.workforce import AutonomyLevel, Department, Readiness
from tests.application.workforce_fixtures import (
    advisor_document,
    automation_document,
    employee_document,
    tool_document,
)


def build(
    employees: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    automations: list[dict[str, Any]] | None = None,
    advisors: list[dict[str, Any]] | None = None,
) -> Registry:
    return Registry.from_documents(
        employees=employees if employees is not None else [employee_document()],
        tools=tools if tools is not None else [tool_document()],
        automations=automations if automations is not None else [automation_document()],
        advisors=advisors if advisors is not None else [advisor_document()],
    )


def test_registry__parses_every_kind_of_document() -> None:
    registry = build()

    shon = registry.employees["shon"]
    assert shon.identity.department is Department.PRODUCT_CATALOG
    assert shon.autonomy is AutonomyLevel.L1
    assert shon.routing.max_cost_per_task == Decimal("0.40")
    assert shon.routing.fallback_tiers == (ModelTier.BALANCED,)
    assert shon.is_production_ready()
    assert registry.tools["catalog.read"].mutating is False
    assert registry.automations["etsy-order-intake"].retry.max_attempts == 3
    assert registry.advisors["jev"].readiness is Readiness.DRAFT


def test_registry__employees_by_department() -> None:
    registry = build(employees=[employee_document("shon"), employee_document("noam")])

    assert [p.identity.id for p in registry.employees_in(Department.PRODUCT_CATALOG)] == [
        "noam",
        "shon",
    ]


def test_registry__draft_employee__parses_without_an_evaluation() -> None:
    registry = build(employees=[employee_document(readiness="draft")])

    assert registry.employees["shon"].incomplete_sections()


def test_registry__duplicate_ids__are_reported() -> None:
    with pytest.raises(RegistryError, match="duplicate employee id 'shon'"):
        build(employees=[employee_document(), employee_document()])


def test_registry__unknown_tool_in_a_profile__is_reported() -> None:
    with pytest.raises(RegistryError, match=r"unknown tool 'orders\.delete'"):
        build(employees=[employee_document(allowed_tools=["orders.delete"])])


def test_registry__unknown_automation_in_a_profile__is_reported() -> None:
    with pytest.raises(RegistryError, match="unknown automation 'nope-flow'"):
        build(employees=[employee_document(allowed_automations=["nope-flow"])])


def test_registry__granted_automation__must_be_callable_as_an_agent_tool() -> None:
    """A grant means "may trigger it as a tool"; ownership is the automation's business_owner."""
    with pytest.raises(RegistryError, match="not callable through agent_tool"):
        build(
            employees=[employee_document(allowed_automations=["etsy-order-intake"])],
            automations=[automation_document(callable_from=["event"])],
        )


def test_registry__tool_owner_must_be_an_employee() -> None:
    with pytest.raises(RegistryError, match="owner 'ghost'"):
        build(tools=[tool_document(owner="ghost")])


def test_registry__automation_owner_must_be_an_employee_or_the_owner() -> None:
    build(automations=[automation_document(business_owner="owner")])

    with pytest.raises(RegistryError, match="business owner 'ghost'"):
        build(automations=[automation_document(business_owner="ghost")])


def test_registry__advisor_is_not_an_employee() -> None:
    """Jev is a shared service: it can never take an employee's identity or headcount."""
    with pytest.raises(RegistryError, match="advisor 'shon' collides"):
        build(advisors=[advisor_document(id="shon")])


def test_registry__invalid_document__names_the_document_and_the_problem() -> None:
    with pytest.raises(RegistryError, match=r"employee 'shon'.*department"):
        build(employees=[employee_document(department="sales")])


def test_registry__missing_field__is_reported() -> None:
    document = employee_document()
    del document["routing"]

    with pytest.raises(RegistryError, match="routing"):
        build(employees=[document])


def test_registry__every_problem_is_reported_at_once() -> None:
    with pytest.raises(RegistryError) as raised:
        build(
            employees=[
                employee_document(allowed_tools=["x.one"]),
                employee_document("noam", allowed_tools=["x.two"]),
            ]
        )

    assert len(raised.value.problems) == 2


def test_registry__wrong_value_type__is_reported() -> None:
    with pytest.raises(RegistryError, match="allowed_tools"):
        build(employees=[employee_document(allowed_tools="catalog.read")])
