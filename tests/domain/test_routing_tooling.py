"""Routing policies and tool definitions: validated values (ADR-0012, ADR-0014)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from htco_domain.routing import AdvisorUsage, DataClass, ModelRoutingPolicy, ModelTier
from htco_domain.tooling import RiskLevel, SideEffect, ToolDefinition, ToolFamily
from htco_domain.workforce import Readiness

POLICY = ModelRoutingPolicy(
    version="v1",
    allowed_providers=frozenset({"anthropic"}),
    required_capabilities=frozenset(),
    default_tier=ModelTier.ECONOMY,
    max_tier=ModelTier.BALANCED,
    max_cost_per_task=Decimal("0.10"),
    max_data_class=DataClass.INTERNAL,
    fallback_tiers=(),
    decision_advisor=AdvisorUsage.NEVER,
)


def test_tiers__escalate_in_order() -> None:
    assert ModelTier.ECONOMY.rank < ModelTier.BALANCED.rank < ModelTier.ADVANCED.rank
    assert ModelTier.ADVANCED.rank < ModelTier.CRITICAL.rank
    assert ModelTier.NO_AI.rank < ModelTier.ECONOMY.rank


def test_data_classes__are_ordered_by_sensitivity() -> None:
    assert DataClass.PUBLIC.rank < DataClass.INTERNAL.rank < DataClass.CONFIDENTIAL.rank
    assert DataClass.CONFIDENTIAL.rank < DataClass.PERSONAL.rank


def test_policy__default_above_max__is_rejected() -> None:
    with pytest.raises(ValueError, match="default tier"):
        replace(POLICY, default_tier=ModelTier.ADVANCED)


def test_policy__fallback_above_max__is_rejected() -> None:
    with pytest.raises(ValueError, match="fallback"):
        replace(POLICY, fallback_tiers=(ModelTier.CRITICAL,))


@pytest.mark.parametrize("cost", [Decimal(0), Decimal(-1)])
def test_policy__non_positive_budget__is_rejected(cost: Decimal) -> None:
    with pytest.raises(ValueError, match="budget"):
        replace(POLICY, max_cost_per_task=cost)


def test_policy__model_tier_without_providers__is_rejected() -> None:
    with pytest.raises(ValueError, match="provider"):
        replace(POLICY, allowed_providers=frozenset())


def test_policy__no_ai_only__needs_no_provider() -> None:
    policy = replace(
        POLICY,
        allowed_providers=frozenset(),
        default_tier=ModelTier.NO_AI,
        max_tier=ModelTier.NO_AI,
    )

    assert policy.max_tier is ModelTier.NO_AI


def test_policy__blank_version__is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        replace(POLICY, version="")


TOOL = ToolDefinition(
    name="orders.read",
    version=1,
    family=ToolFamily.COMMERCE_CATALOG,
    owner="alex",
    side_effect=SideEffect.NONE,
    risk=RiskLevel.LOW,
    data_scope=DataClass.INTERNAL,
    audit_required=False,
    readiness=Readiness.DRAFT,
    description="Read orders of one shop.",
)


def test_tool_families__are_the_eleven_initial_families_plus_office_crm() -> None:
    assert len(ToolFamily) == 12
    assert ToolFamily.OFFICE_CRM in ToolFamily


def test_tool__read_only__is_not_mutating() -> None:
    assert TOOL.mutating is False
    assert replace(TOOL, side_effect=SideEffect.INTERNAL).mutating is True


@pytest.mark.parametrize("name", ["orders", "Orders.read", "orders.", "orders read"])
def test_tool__invalid_name__is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="tool name"):
        replace(TOOL, name=name)


@pytest.mark.parametrize("effect", [SideEffect.EXTERNAL, SideEffect.FINANCIAL])
def test_tool__external_or_financial_without_audit__is_rejected(effect: SideEffect) -> None:
    with pytest.raises(ValueError, match="audit"):
        replace(TOOL, side_effect=effect, risk=RiskLevel.HIGH)


@pytest.mark.parametrize("effect", [SideEffect.EXTERNAL, SideEffect.FINANCIAL])
def test_tool__external_or_financial_low_risk__is_rejected(effect: SideEffect) -> None:
    with pytest.raises(ValueError, match="risk"):
        replace(TOOL, side_effect=effect, audit_required=True)


def test_tool__version_below_one__is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        replace(TOOL, version=0)


def test_risk__levels_are_ordered() -> None:
    assert RiskLevel.LOW.rank < RiskLevel.MEDIUM.rank < RiskLevel.HIGH.rank
    assert RiskLevel.HIGH.rank < RiskLevel.CRITICAL.rank
