"""The central model router: one deterministic router, one policy per employee (FR-MDL-02).

Every employee's request goes through `route` with that employee's versioned policy. The
router first asks whether the task needs a model at all (`NO_AI`), then picks the cheapest
healthy model in the required tier that satisfies the policy's providers, capabilities, data
class and per-task budget, falling back through the policy's fallback tiers. The decision is a
value the caller records (`routing_decisions`, FR-MDL-05); model names come from the catalog,
never from agent code.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from htco_domain.routing import DataClass, ModelCapability, ModelRoutingPolicy, ModelTier


@dataclass(frozen=True)
class ModelOffer:
    """One model from the verified catalog (ADR-0004)."""

    provider: str
    model_id: str
    tier: ModelTier
    capabilities: frozenset[ModelCapability]
    max_data_class: DataClass
    estimated_cost_per_call: Decimal
    healthy: bool = True


@dataclass(frozen=True)
class RoutingRequest:
    employee_id: str
    task_type: str
    required_capabilities: frozenset[ModelCapability]
    data_class: DataClass
    minimum_tier: ModelTier  # how hard the task is, as judged by rules or the advisor
    deterministic: bool  # true when code can do it: no model call (Zero-AI first)


@dataclass(frozen=True)
class RoutingDecision:
    employee_id: str
    policy_version: str
    tier: ModelTier
    offer: ModelOffer | None
    fallbacks: tuple[ModelOffer, ...]
    reason: str


class RoutingRefused(Exception):  # noqa: N818 - reads as "routing (was) refused"
    pass


def _candidates(
    policy: ModelRoutingPolicy,
    request: RoutingRequest,
    tier: ModelTier,
    catalog: Sequence[ModelOffer],
) -> list[ModelOffer]:
    needed = policy.required_capabilities | request.required_capabilities
    matching = [
        offer
        for offer in catalog
        if offer.tier is tier
        and offer.healthy
        and offer.provider in policy.allowed_providers
        and needed <= offer.capabilities
        and offer.max_data_class.rank >= request.data_class.rank
        and offer.estimated_cost_per_call <= policy.max_cost_per_task
    ]
    return sorted(matching, key=lambda offer: (offer.estimated_cost_per_call, offer.model_id))


def route(
    policy: ModelRoutingPolicy, request: RoutingRequest, catalog: Sequence[ModelOffer]
) -> RoutingDecision:
    if request.deterministic:
        return RoutingDecision(
            request.employee_id, policy.version, ModelTier.NO_AI, None, (), "deterministic task"
        )
    if request.data_class.rank > policy.max_data_class.rank:
        raise RoutingRefused(
            f"{request.employee_id}: data class {request.data_class.value} is above the "
            f"policy's {policy.max_data_class.value}"
        )
    tier = max(policy.default_tier, request.minimum_tier, key=lambda t: t.rank)
    if tier.rank > policy.max_tier.rank:
        raise RoutingRefused(
            f"{request.employee_id}: task needs {tier.value}, above the policy max tier "
            f"{policy.max_tier.value}"
        )

    # Fallback tiers weaker than the task needs are never used.
    tiers = [tier, *(t for t in policy.fallback_tiers if t.rank >= request.minimum_tier.rank)]
    ranked = [
        offer for candidate in tiers for offer in _candidates(policy, request, candidate, catalog)
    ]
    if not ranked:
        raise RoutingRefused(
            f"{request.employee_id}: no model in the catalog satisfies policy {policy.version}"
        )
    chosen, *fallbacks = list(dict.fromkeys(ranked))
    return RoutingDecision(
        employee_id=request.employee_id,
        policy_version=policy.version,
        tier=chosen.tier,
        offer=chosen,
        fallbacks=tuple(fallbacks),
        reason=f"cheapest {chosen.tier.value} model satisfying policy {policy.version}",
    )
