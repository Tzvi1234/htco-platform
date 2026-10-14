"""One central router, one policy per employee (Scenario E, FR-MDL-02)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from htco_application.routing.router import (
    ModelOffer,
    RoutingRefused,
    RoutingRequest,
    route,
)
from htco_domain.routing import (
    AdvisorUsage,
    DataClass,
    ModelCapability,
    ModelRoutingPolicy,
    ModelTier,
)

C = ModelCapability
CATALOG = (
    ModelOffer(
        "anthropic",
        "economy-a",
        ModelTier.ECONOMY,
        frozenset({C.TEXT, C.STRUCTURED_OUTPUT}),
        DataClass.CONFIDENTIAL,
        Decimal("0.002"),
    ),
    ModelOffer(
        "openai",
        "economy-o",
        ModelTier.ECONOMY,
        frozenset({C.TEXT, C.STRUCTURED_OUTPUT}),
        DataClass.INTERNAL,
        Decimal("0.001"),
    ),
    ModelOffer(
        "anthropic",
        "balanced-a",
        ModelTier.BALANCED,
        frozenset({C.TEXT, C.STRUCTURED_OUTPUT, C.TOOL_USE}),
        DataClass.CONFIDENTIAL,
        Decimal("0.02"),
    ),
    ModelOffer(
        "anthropic",
        "vision-a",
        ModelTier.VISION,
        frozenset({C.TEXT, C.VISION}),
        DataClass.INTERNAL,
        Decimal("0.03"),
    ),
    ModelOffer(
        "anthropic",
        "advanced-a",
        ModelTier.ADVANCED,
        frozenset({C.TEXT, C.STRUCTURED_OUTPUT, C.TOOL_USE, C.REASONING}),
        DataClass.CONFIDENTIAL,
        Decimal("0.20"),
    ),
)

# Dana (customer service) handles personal data on a tight budget, Anthropic only.
DANA = ModelRoutingPolicy(
    version="dana-routing-v1",
    allowed_providers=frozenset({"anthropic"}),
    required_capabilities=frozenset({C.STRUCTURED_OUTPUT}),
    default_tier=ModelTier.ECONOMY,
    max_tier=ModelTier.BALANCED,
    max_cost_per_task=Decimal("0.05"),
    max_data_class=DataClass.CONFIDENTIAL,
    fallback_tiers=(ModelTier.BALANCED,),
    decision_advisor=AdvisorUsage.WHEN_RULES_INCONCLUSIVE,
)
# Alex (engineering) needs reasoning and tools, any approved provider, no customer data.
ALEX = ModelRoutingPolicy(
    version="alex-routing-v1",
    allowed_providers=frozenset({"anthropic", "openai"}),
    required_capabilities=frozenset({C.TOOL_USE, C.REASONING}),
    default_tier=ModelTier.ADVANCED,
    max_tier=ModelTier.CRITICAL,
    max_cost_per_task=Decimal("2.00"),
    max_data_class=DataClass.INTERNAL,
    fallback_tiers=(),
    decision_advisor=AdvisorUsage.NEVER,
)


def a_request(employee: str, **changes: object) -> RoutingRequest:
    request = RoutingRequest(
        employee_id=employee,
        task_type="draft",
        required_capabilities=frozenset(),
        data_class=DataClass.INTERNAL,
        minimum_tier=ModelTier.ECONOMY,
        deterministic=False,
    )
    return replace(request, **changes)  # type: ignore[arg-type]


@pytest.mark.ac("SCN-E")
def test_two_employees__same_router__their_own_policies() -> None:
    dana = route(DANA, a_request("dana"), CATALOG)
    alex = route(ALEX, a_request("alex"), CATALOG)

    assert (dana.offer, alex.offer) == (CATALOG[0], CATALOG[4])
    assert dana.offer is not None
    assert dana.offer.provider == "anthropic"
    assert (dana.policy_version, alex.policy_version) == ("dana-routing-v1", "alex-routing-v1")
    assert (dana.employee_id, alex.employee_id) == ("dana", "alex")


@pytest.mark.ac("SCN-E")
def test_privacy__data_above_the_policy_class__is_refused() -> None:
    with pytest.raises(RoutingRefused, match="data class"):
        route(ALEX, a_request("alex", data_class=DataClass.PERSONAL), CATALOG)


def test_privacy__models_not_cleared_for_the_data__are_skipped() -> None:
    decision = route(
        replace(DANA, allowed_providers=frozenset({"anthropic", "openai"})),
        a_request("dana", data_class=DataClass.CONFIDENTIAL),
        CATALOG,
    )

    assert decision.offer == CATALOG[0]  # the cheaper OpenAI model is cleared for internal only


@pytest.mark.ac("SCN-E")
def test_budget__a_tier_over_the_per_task_budget__falls_back_or_refuses() -> None:
    with pytest.raises(RoutingRefused, match="no model"):
        route(
            replace(DANA, fallback_tiers=()),
            a_request("dana", required_capabilities=frozenset({C.TOOL_USE})),
            CATALOG,
        )


def test_capabilities__missing_on_the_default_tier__use_the_fallback_tier() -> None:
    decision = route(
        replace(DANA, max_cost_per_task=Decimal("0.10")),
        a_request("dana", required_capabilities=frozenset({C.TOOL_USE})),
        CATALOG,
    )

    assert decision.offer == CATALOG[2]
    assert decision.tier is ModelTier.BALANCED


def test_deterministic_task__routes_to_no_ai() -> None:
    decision = route(DANA, a_request("dana", deterministic=True), CATALOG)

    assert decision.tier is ModelTier.NO_AI
    assert decision.offer is None


def test_harder_task__raises_the_tier_up_to_the_policy_max() -> None:
    decision = route(
        ALEX,
        a_request("alex", minimum_tier=ModelTier.CRITICAL),
        (
            *CATALOG,
            ModelOffer(
                "anthropic",
                "critical-a",
                ModelTier.CRITICAL,
                frozenset({C.TOOL_USE, C.REASONING}),
                DataClass.INTERNAL,
                Decimal("1.00"),
            ),
        ),
    )

    assert decision.tier is ModelTier.CRITICAL


def test_task_above_the_policy_max_tier__is_refused() -> None:
    with pytest.raises(RoutingRefused, match="max tier"):
        route(DANA, a_request("dana", minimum_tier=ModelTier.ADVANCED), CATALOG)


def test_unhealthy_model__is_skipped_and_others_become_fallbacks() -> None:
    catalog = (replace(CATALOG[0], healthy=False), *CATALOG[1:])

    decision = route(replace(DANA, max_cost_per_task=Decimal("0.10")), a_request("dana"), catalog)

    assert decision.offer == CATALOG[2]


def test_fallbacks__list_the_remaining_candidates_in_order() -> None:
    decision = route(
        replace(ALEX, default_tier=ModelTier.ECONOMY, required_capabilities=frozenset()),
        a_request("alex"),
        CATALOG,
    )

    assert decision.offer == CATALOG[1]
    assert decision.fallbacks == (CATALOG[0],)


def test_fallback_tiers_below_the_task_difficulty__are_never_used() -> None:
    policy = replace(DANA, default_tier=ModelTier.ECONOMY, fallback_tiers=(ModelTier.ECONOMY,))
    catalog = tuple(offer for offer in CATALOG if offer.tier is not ModelTier.BALANCED)

    with pytest.raises(RoutingRefused, match="no model"):
        route(policy, a_request("dana", minimum_tier=ModelTier.BALANCED), catalog)
