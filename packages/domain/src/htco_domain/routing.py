"""Model routing policies: one versioned policy per employee, one central router (ADR-0012).

A policy names capability tiers, never model names; the model catalog maps tiers to verified
provider models (ADR-0004).
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ModelTier(StrEnum):
    NO_AI = "no_ai"  # deterministic code; no model call at all
    ECONOMY = "economy"
    BALANCED = "balanced"
    ADVANCED = "advanced"
    CRITICAL = "critical"
    VISION = "vision"
    IMAGE = "image"

    @property
    def rank(self) -> int:
        """Escalation order. Vision and image are modalities, ranked with balanced."""
        return _TIER_RANK[self]


_TIER_RANK = {
    ModelTier.NO_AI: 0,
    ModelTier.ECONOMY: 1,
    ModelTier.BALANCED: 2,
    ModelTier.VISION: 2,
    ModelTier.IMAGE: 2,
    ModelTier.ADVANCED: 3,
    ModelTier.CRITICAL: 4,
}


class ModelCapability(StrEnum):
    TEXT = "text"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_USE = "tool_use"
    VISION = "vision"
    IMAGE_GENERATION = "image_generation"
    LONG_CONTEXT = "long_context"
    REASONING = "reasoning"


class DataClass(StrEnum):
    """How sensitive the data in a request is; a policy caps what may reach a model."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PERSONAL = "personal"  # customer PII; masked unless the task needs it (FR-MDL-07)

    @property
    def rank(self) -> int:
        return list(DataClass).index(self)


class AdvisorUsage(StrEnum):
    """When an employee may ask the shared Decision Advisor."""

    NEVER = "never"
    WHEN_RULES_INCONCLUSIVE = "when_rules_inconclusive"
    PREFERRED = "preferred"


@dataclass(frozen=True)
class ModelRoutingPolicy:
    version: str
    allowed_providers: frozenset[str]
    required_capabilities: frozenset[ModelCapability]
    default_tier: ModelTier
    max_tier: ModelTier
    max_cost_per_task: Decimal
    max_data_class: DataClass
    fallback_tiers: tuple[ModelTier, ...]
    decision_advisor: AdvisorUsage

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("routing policy version must not be blank")
        if self.default_tier.rank > self.max_tier.rank:
            raise ValueError("default tier is above the policy's max tier")
        if any(tier.rank > self.max_tier.rank for tier in self.fallback_tiers):
            raise ValueError("a fallback tier is above the policy's max tier")
        if self.max_cost_per_task <= 0:
            raise ValueError("the per-task budget must be positive")
        if self.max_tier is not ModelTier.NO_AI and not self.allowed_providers:
            raise ValueError("a policy that may call models needs at least one provider")
