"""Least-privilege tool access for employees, decided by the backend (FR-AGT-04, ADR-0014).

Whatever a model asks for, an employee reaches only the tools its current profile grants, and
only while it is production-ready. External and financial tools need an approval below L3;
critical tools always do. Nothing here accepts advice: a decision advisor can rank options but
never widens access.
"""

from dataclasses import dataclass
from enum import StrEnum

from htco_application.workforce.registry import Registry
from htco_domain.tooling import RiskLevel, SideEffect
from htco_domain.workforce import PRODUCTION_READINESS, AutonomyLevel


class ToolVerdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class ToolAccessDecision:
    verdict: ToolVerdict
    reason: str


def _deny(reason: str) -> ToolAccessDecision:
    return ToolAccessDecision(ToolVerdict.DENY, reason)


def _denial(registry: Registry, employee_id: str, tool_name: str) -> str | None:
    """Why the call is denied outright, or None when it may proceed to the risk check."""
    profile = registry.employees.get(employee_id)
    tool = registry.tools.get(tool_name)
    if profile is None:
        return f"unknown employee {employee_id!r}"
    if tool is None:
        return f"unknown tool {tool_name!r}"
    if not profile.is_production_ready():
        return f"{employee_id} is not active ({profile.readiness.value})"
    if tool_name not in profile.allowed_tools:
        return f"{tool_name} is not granted to {employee_id}"
    if tool.readiness not in PRODUCTION_READINESS:
        return f"tool {tool_name} is not ready ({tool.readiness.value})"
    return None


def decide_tool_access(registry: Registry, employee_id: str, tool_name: str) -> ToolAccessDecision:
    denial = _denial(registry, employee_id, tool_name)
    if denial is not None:
        return _deny(denial)
    profile, tool = registry.employees[employee_id], registry.tools[tool_name]
    if tool.risk is RiskLevel.CRITICAL:
        return ToolAccessDecision(ToolVerdict.REQUIRE_APPROVAL, f"{tool_name} is critical risk")
    if tool.side_effect in {SideEffect.EXTERNAL, SideEffect.FINANCIAL} and (
        profile.autonomy < AutonomyLevel.L3
    ):
        return ToolAccessDecision(
            ToolVerdict.REQUIRE_APPROVAL,
            f"{tool_name} acts outside HTCO and {employee_id} is {profile.autonomy.name}",
        )
    return ToolAccessDecision(ToolVerdict.ALLOW, f"{tool_name} is granted to {employee_id}")
