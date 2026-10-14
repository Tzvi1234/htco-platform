"""Continuous optimization: proposals from detection to verified result (ADR-0012).

The office improves itself through these records: a proposal carries its evidence, acceptance
criteria and rollback plan, and is verified by measurement after deployment.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from htco_domain.actors import ActorRef
from htco_domain.state_machine import check_transition


class ProposalState(StrEnum):
    DETECTED = "detected"
    PROPOSED = "proposed"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEVELOPMENT = "development"
    TESTING = "testing"
    DEPLOYED = "deployed"
    MEASURING = "measuring"
    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"


_P = ProposalState
_PROPOSAL_TRANSITIONS: Mapping[ProposalState, frozenset[ProposalState]] = {
    _P.DETECTED: frozenset({_P.PROPOSED, _P.REJECTED}),
    _P.PROPOSED: frozenset({_P.EVALUATING, _P.REJECTED}),
    _P.EVALUATING: frozenset({_P.APPROVED, _P.REJECTED}),
    _P.APPROVED: frozenset({_P.DEVELOPMENT}),
    _P.DEVELOPMENT: frozenset({_P.TESTING}),
    # A failed test sends the work back to development.
    _P.TESTING: frozenset({_P.DEPLOYED, _P.DEVELOPMENT}),
    _P.DEPLOYED: frozenset({_P.MEASURING, _P.ROLLED_BACK}),
    _P.MEASURING: frozenset({_P.VERIFIED, _P.ROLLED_BACK}),
}


@dataclass(frozen=True)
class OptimizationProposal:
    id: uuid.UUID
    title: str
    detected_by: ActorRef
    state: ProposalState
    problem: str
    deterministic_candidate: bool  # whether the work could become a deterministic automation
    acceptance_criteria: tuple[str, ...]
    rollback_plan: str

    def transition(self, target: ProposalState) -> OptimizationProposal:
        check_transition("optimization proposal", _PROPOSAL_TRANSITIONS, self.state, target)
        if target is ProposalState.PROPOSED:
            if not self.acceptance_criteria:
                raise ValueError("a proposal needs measurable acceptance criteria")
            if not self.rollback_plan.strip():
                raise ValueError("a proposal needs a rollback plan")
        return replace(self, state=target)
