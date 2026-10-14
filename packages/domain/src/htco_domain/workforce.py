"""The AI workforce: employee identities and versioned operational profiles (ADR-0012).

An employee is not a prompt and not a graph node. Its identity is permanent; its profile
(responsibilities, tools, automations, routing policy, autonomy, specification) is versioned
and reviewable. A profile becomes production-ready only through a passed evaluation with every
specification section defined: a configuration file existing is never enough.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

from htco_domain.routing import ModelRoutingPolicy


class Department(StrEnum):
    EXECUTIVE_OFFICE = "executive_office"
    PRODUCT_CATALOG = "product_research_content_catalog"
    PROCUREMENT_FINANCE = "procurement_finance"
    ORDER_ASSURANCE = "order_assurance"
    MARKETING = "marketing"
    CUSTOMER_SERVICE = "customer_service"
    ENGINEERING = "engineering"


class AutonomyLevel(IntEnum):
    """L0 suggests, L1 acts inside HTCO, L2 acts outside after approval, L3 within a policy."""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3


class Readiness(StrEnum):
    DRAFT = "draft"  # scope only; sections still being designed
    SPECIFIED = "specified"  # every section written, not yet evaluated
    EVALUATING = "evaluating"
    READY = "ready"  # evaluation passed; may be activated
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


PRODUCTION_READINESS = frozenset({Readiness.READY, Readiness.ACTIVE})


class SectionStatus(StrEnum):
    DRAFT = "draft"
    DEFINED = "defined"


# The Agent Specification Sheet: every employee profile has exactly these sections.
EMPLOYEE_SPEC_SECTIONS: tuple[str, ...] = (
    "identity",
    "mission",
    "business_goals",
    "responsibilities",
    "non_responsibilities",
    "input_schemas",
    "output_schemas",
    "triggers",
    "allowed_tools",
    "allowed_automations",
    "skills",
    "business_knowledge",
    "memory",
    "communication",
    "model_routing",
    "decision_advisor",
    "budgets",
    "risk_and_approvals",
    "evaluation",
    "failure_handling",
    "versioning",
    "performance_metrics",
)

_EMPLOYEE_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


@dataclass(frozen=True)
class SpecSection:
    status: SectionStatus
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a specification section needs text (say what is still open)")


def check_specification(
    specification: Mapping[str, SpecSection], required: tuple[str, ...]
) -> tuple[str, ...]:
    """Validate a sheet's section names; return the sections still in draft."""
    missing = [name for name in required if name not in specification]
    if missing:
        raise ValueError(f"specification is missing sections: {', '.join(missing)}")
    unknown = sorted(set(specification) - set(required))
    if unknown:
        raise ValueError(f"specification has unknown sections: {', '.join(unknown)}")
    return tuple(name for name in required if specification[name].status is SectionStatus.DRAFT)


@dataclass(frozen=True)
class EvaluationResult:
    suite: str
    version: str
    passed: bool
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at needs a timezone")


@dataclass(frozen=True)
class EmployeeIdentity:
    """Permanent: renaming a title or changing a profile never changes who the employee is."""

    id: str
    name: str
    title: str
    department: Department

    def __post_init__(self) -> None:
        if not _EMPLOYEE_ID.match(self.id):
            raise ValueError(f"invalid employee id {self.id!r}")
        if not self.name.strip() or not self.title.strip():
            raise ValueError("an employee needs a name and title")


@dataclass(frozen=True)
class EmployeeProfile:
    identity: EmployeeIdentity
    version: int
    autonomy: AutonomyLevel
    readiness: Readiness
    allowed_tools: frozenset[str]
    allowed_automations: frozenset[str]
    routing: ModelRoutingPolicy
    specification: Mapping[str, SpecSection]
    evaluation: EvaluationResult | None
    activation_phase: int

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("profile version starts at 1")
        drafts = check_specification(self.specification, EMPLOYEE_SPEC_SECTIONS)
        if self.readiness in PRODUCTION_READINESS:
            if self.evaluation is None or not self.evaluation.passed:
                raise ValueError(
                    f"{self.identity.id}: {self.readiness.value} needs a passed evaluation"
                )
            if drafts:
                raise ValueError(
                    f"{self.identity.id}: {self.readiness.value} with draft sections: "
                    + ", ".join(drafts)
                )

    def incomplete_sections(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in EMPLOYEE_SPEC_SECTIONS
            if self.specification[name].status is SectionStatus.DRAFT
        )

    def is_production_ready(self) -> bool:
        return self.readiness in PRODUCTION_READINESS
