"""The workforce registry: employees, tools, automations and advisors as reviewed data.

The office is configuration, not code: each employee, tool and automation is a versioned
document under `registry/` (TOML in the repository; database rows from Phase 6). This module
turns parsed documents into validated domain objects and checks that they fit together (every
granted tool exists, every owner is an employee, an advisor never takes an employee's place).
It reports every problem at once, so a review sees the whole picture.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from htco_domain.automation import AutomationDefinition, RetryPolicy, TriggerKind
from htco_domain.routing import (
    AdvisorUsage,
    DataClass,
    ModelCapability,
    ModelRoutingPolicy,
    ModelTier,
)
from htco_domain.tooling import RiskLevel, SideEffect, ToolDefinition, ToolFamily
from htco_domain.workforce import (
    AutonomyLevel,
    Department,
    EmployeeIdentity,
    EmployeeProfile,
    EvaluationResult,
    Readiness,
    SectionStatus,
    SpecSection,
)

type Document = Mapping[str, Any]

# Automations may be owned by an employee or by the owner of the business.
OWNER = "owner"


class RegistryError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("invalid registry:\n" + "\n".join(f"- {p}" for p in self.problems))


@dataclass(frozen=True)
class AdvisorProfile:
    """A shared decision service (Jev): an identity in the office, never an employee."""

    id: str
    name: str
    title: str
    providers: tuple[str, ...]
    readiness: Readiness
    description: str


def _field(document: Document, key: str) -> Any:
    if key not in document:
        raise ValueError(f"missing field {key!r}")
    return document[key]


def _text(document: Document, key: str) -> str:
    value = _field(document, key)
    if not isinstance(value, str):
        raise TypeError(f"{key!r} must be text")
    return value


def _integer(document: Document, key: str) -> int:
    value = _field(document, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key!r} must be an integer")
    return value


def _flag(document: Document, key: str) -> bool:
    value = _field(document, key)
    if not isinstance(value, bool):
        raise TypeError(f"{key!r} must be true or false")
    return value


def _texts(document: Document, key: str) -> tuple[str, ...]:
    value = _field(document, key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key!r} must be a list of text")
    return tuple(value)


def _table(document: Document, key: str) -> Document:
    value = _field(document, key)
    if not isinstance(value, Mapping):
        raise TypeError(f"{key!r} must be a table")
    return value


def _enum[E: Enum](kind: type[E], document: Document, key: str) -> E:
    raw = _text(document, key)
    try:
        return kind(raw)
    except ValueError:
        allowed = ", ".join(str(member.value) for member in kind)
        raise ValueError(f"{key!r} is {raw!r}; expected one of {allowed}") from None


def _enums[E: Enum](kind: type[E], document: Document, key: str) -> tuple[E, ...]:
    return tuple(_enum(kind, {key: raw}, key) for raw in _texts(document, key))


def _decimal(document: Document, key: str) -> Decimal:
    try:
        return Decimal(_text(document, key))
    except InvalidOperation:
        raise ValueError(f"{key!r} must be a decimal number in quotes") from None


def _specification(document: Document) -> dict[str, SpecSection]:
    return {
        name: SpecSection(_enum(SectionStatus, section, "status"), _text(section, "text"))
        for name, section in _table(document, "specification").items()
    }


def _routing(document: Document) -> ModelRoutingPolicy:
    return ModelRoutingPolicy(
        version=_text(document, "version"),
        allowed_providers=frozenset(_texts(document, "allowed_providers")),
        required_capabilities=frozenset(_enums(ModelCapability, document, "required_capabilities")),
        default_tier=_enum(ModelTier, document, "default_tier"),
        max_tier=_enum(ModelTier, document, "max_tier"),
        max_cost_per_task=_decimal(document, "max_cost_per_task"),
        max_data_class=_enum(DataClass, document, "max_data_class"),
        fallback_tiers=_enums(ModelTier, document, "fallback_tiers"),
        decision_advisor=_enum(AdvisorUsage, document, "decision_advisor"),
    )


def _evaluation(document: Document) -> EvaluationResult | None:
    if "evaluation" not in document:
        return None
    evaluation = _table(document, "evaluation")
    return EvaluationResult(
        suite=_text(evaluation, "suite"),
        version=_text(evaluation, "version"),
        passed=_flag(evaluation, "passed"),
        evaluated_at=datetime.fromisoformat(_text(evaluation, "evaluated_at")),
    )


def parse_employee(document: Document) -> EmployeeProfile:
    return EmployeeProfile(
        identity=EmployeeIdentity(
            id=_text(document, "id"),
            name=_text(document, "name"),
            title=_text(document, "title"),
            department=_enum(Department, document, "department"),
        ),
        version=_integer(document, "version"),
        autonomy=AutonomyLevel[_text(document, "autonomy")],
        readiness=_enum(Readiness, document, "readiness"),
        allowed_tools=frozenset(_texts(document, "allowed_tools")),
        allowed_automations=frozenset(_texts(document, "allowed_automations")),
        routing=_routing(_table(document, "routing")),
        specification=_specification(document),
        evaluation=_evaluation(document),
        activation_phase=_integer(document, "activation_phase"),
    )


def parse_tool(document: Document) -> ToolDefinition:
    return ToolDefinition(
        name=_text(document, "name"),
        version=_integer(document, "version"),
        family=_enum(ToolFamily, document, "family"),
        owner=_text(document, "owner"),
        side_effect=_enum(SideEffect, document, "side_effect"),
        risk=_enum(RiskLevel, document, "risk"),
        data_scope=_enum(DataClass, document, "data_scope"),
        audit_required=_flag(document, "audit_required"),
        readiness=_enum(Readiness, document, "readiness"),
        description=_text(document, "description"),
    )


def parse_automation(document: Document) -> AutomationDefinition:
    retry = _table(document, "retry")
    return AutomationDefinition(
        id=_text(document, "id"),
        name=_text(document, "name"),
        version=_integer(document, "version"),
        business_owner=_text(document, "business_owner"),
        primary_trigger=_enum(TriggerKind, document, "primary_trigger"),
        callable_from=frozenset(_enums(TriggerKind, document, "callable_from")),
        retry=RetryPolicy(
            max_attempts=_integer(retry, "max_attempts"),
            initial_backoff=timedelta(seconds=_integer(retry, "initial_backoff_seconds")),
            max_backoff=timedelta(seconds=_integer(retry, "max_backoff_seconds")),
        ),
        timeout=timedelta(seconds=_integer(document, "timeout_seconds")),
        side_effect=_enum(SideEffect, document, "side_effect"),
        approval_required=_flag(document, "approval_required"),
        ai_steps=_texts(document, "ai_steps"),
        readiness=_enum(Readiness, document, "readiness"),
        specification=_specification(document),
        activation_phase=_integer(document, "activation_phase"),
    )


def parse_advisor(document: Document) -> AdvisorProfile:
    return AdvisorProfile(
        id=_text(document, "id"),
        name=_text(document, "name"),
        title=_text(document, "title"),
        providers=_texts(document, "providers"),
        readiness=_enum(Readiness, document, "readiness"),
        description=_text(document, "description"),
    )


def _parse_all[T](
    kind: str,
    documents: Iterable[Document],
    parse: Callable[[Document], T],
    key: Callable[[T], str],
    problems: list[str],
) -> dict[str, T]:
    parsed: dict[str, T] = {}
    for document in documents:
        label = document.get("id") or document.get("name") or "?"
        try:
            item = parse(document)
        except (ValueError, KeyError, TypeError) as error:
            problems.append(f"{kind} {label!r}: {error}")
            continue
        if key(item) in parsed:
            problems.append(f"duplicate {kind} id {key(item)!r}")
            continue
        parsed[key(item)] = item
    return parsed


@dataclass(frozen=True)
class Registry:
    employees: Mapping[str, EmployeeProfile]
    tools: Mapping[str, ToolDefinition]
    automations: Mapping[str, AutomationDefinition]
    advisors: Mapping[str, AdvisorProfile]

    @classmethod
    def from_documents(
        cls,
        *,
        employees: Iterable[Document],
        tools: Iterable[Document],
        automations: Iterable[Document],
        advisors: Iterable[Document],
    ) -> Registry:
        problems: list[str] = []
        employee_map = _parse_all(
            "employee", employees, parse_employee, lambda p: p.identity.id, problems
        )
        tool_map = _parse_all("tool", tools, parse_tool, lambda t: t.name, problems)
        automation_map = _parse_all(
            "automation", automations, parse_automation, lambda a: a.id, problems
        )
        advisor_map = _parse_all("advisor", advisors, parse_advisor, lambda a: a.id, problems)

        for profile in employee_map.values():
            employee = profile.identity.id
            problems += [
                f"employee {employee!r}: unknown tool {tool!r}"
                for tool in sorted(profile.allowed_tools - tool_map.keys())
            ]
            problems += [
                f"employee {employee!r}: unknown automation {automation!r}"
                for automation in sorted(profile.allowed_automations - automation_map.keys())
            ]
            problems += [
                f"employee {employee!r}: automation {automation!r} is not callable through "
                "agent_tool"
                for automation in sorted(profile.allowed_automations & automation_map.keys())
                if TriggerKind.AGENT_TOOL not in automation_map[automation].callable_from
            ]
        problems += [
            f"tool {tool.name!r}: owner {tool.owner!r} is not an employee"
            for tool in tool_map.values()
            if tool.owner not in employee_map
        ]
        problems += [
            f"automation {a.id!r}: business owner {a.business_owner!r} is not an employee"
            for a in automation_map.values()
            if a.business_owner != OWNER and a.business_owner not in employee_map
        ]
        problems += [
            f"advisor {advisor!r} collides with an employee id"
            for advisor in advisor_map
            if advisor in employee_map
        ]
        if problems:
            raise RegistryError(problems)
        return cls(
            employees=MappingProxyType(employee_map),
            tools=MappingProxyType(tool_map),
            automations=MappingProxyType(automation_map),
            advisors=MappingProxyType(advisor_map),
        )

    def employees_in(self, department: Department) -> list[EmployeeProfile]:
        return sorted(
            (p for p in self.employees.values() if p.identity.department is department),
            key=lambda p: p.identity.id,
        )
