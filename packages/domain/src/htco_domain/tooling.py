"""The AI Tools Department: versioned tool definitions with risk and side effects (ADR-0014).

A tool exposes a capability; it never decides a business objective. Read-only and mutating
tools are separate definitions, so access can be granted to one without the other.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from htco_domain.routing import DataClass
from htco_domain.workforce import Readiness


class ToolFamily(StrEnum):
    # The office's own records: tasks, approvals, incidents, activity, messages, proposals.
    OFFICE_CRM = "office_crm"
    COMMERCE_CATALOG = "commerce_catalog"
    SUPPLIER_RESEARCH = "supplier_research"
    RESEARCH_RETRIEVAL = "research_retrieval"
    FINANCE_PRICING = "finance_pricing"
    CREATIVE_MEDIA = "creative_media"
    COMMUNICATION_EMAIL = "communication_email"
    GITHUB_DEVELOPMENT = "github_development"
    INFRASTRUCTURE_OBSERVABILITY = "infrastructure_observability"
    DECISION_ADVISOR = "decision_advisor"
    MODEL_GATEWAY = "model_gateway"
    AUTOMATION_EXECUTION = "automation_execution"


class SideEffect(StrEnum):
    NONE = "none"  # reads only
    INTERNAL = "internal"  # changes HTCO's own records
    EXTERNAL = "external"  # changes something outside HTCO or contacts someone
    FINANCIAL = "financial"  # spends or moves money


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(RiskLevel).index(self)


_TOOL_NAME = re.compile(r"^[a-z]+(\.[a-z_]+)+$")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: int
    family: ToolFamily
    owner: str  # the employee (usually Iris or Alex) accountable for the tool
    side_effect: SideEffect
    risk: RiskLevel
    data_scope: DataClass
    audit_required: bool
    readiness: Readiness
    description: str

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r} (expected family.action)")
        if self.version < 1:
            raise ValueError("tool version starts at 1")
        if self.side_effect in {SideEffect.EXTERNAL, SideEffect.FINANCIAL}:
            if not self.audit_required:
                raise ValueError(f"{self.name}: external and financial tools must be audited")
            if self.risk is RiskLevel.LOW:
                raise ValueError(
                    f"{self.name}: external and financial tools are at least medium risk"
                )

    @property
    def mutating(self) -> bool:
        return self.side_effect is not SideEffect.NONE
