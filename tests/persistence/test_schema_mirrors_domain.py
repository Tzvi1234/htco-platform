"""The database's closed lists match the domain enums, so a new role or actor kind cannot drift."""

import re
from pathlib import Path

from htco_application.execution.gateway import ActionOutcome, Channel
from htco_domain.actions import ActionState
from htco_domain.actors import ActorKind
from htco_domain.automation import ExecutionState, TriggerKind
from htco_domain.guards import BreakerState
from htco_domain.identity import Role
from htco_domain.integrations import (
    CapabilityState,
    IntegrationProvider,
    IntegrationStatus,
    ShopCapability,
)
from htco_domain.jobs import JobState
from htco_domain.workforce import Readiness

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def _check_values(constraint: str) -> set[str]:
    sql = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql")))
    # Migrations run in file order, so the last definition of a constraint is the effective one.
    matches = re.findall(rf"constraint {constraint} check \(\s*\w+ in \(([^)]*)\)", sql)
    assert matches, f"check constraint {constraint} not found in the migrations"
    return set(re.findall(r"'([^']+)'", matches[-1]))


def test_memberships_role_check__lists_exactly_the_domain_roles() -> None:
    assert _check_values("memberships_role_check") == {role.value for role in Role}


def test_audit_actor_kind_check__lists_exactly_the_domain_actor_kinds() -> None:
    assert _check_values("audit_log_actor_kind_check") == {kind.value for kind in ActorKind}


def test_shop_integrations_status_check__lists_exactly_the_domain_statuses() -> None:
    assert _check_values("shop_integrations_status_check") == {s.value for s in IntegrationStatus}


def test_shop_integrations_provider_check__lists_exactly_the_domain_providers() -> None:
    assert _check_values("shop_integrations_provider_check") == {
        provider.value for provider in IntegrationProvider
    }


def test_shop_capabilities_capability_check__lists_exactly_the_domain_capabilities() -> None:
    assert _check_values("shop_capabilities_capability_check") == {c.value for c in ShopCapability}


def test_shop_capabilities_state_check__lists_exactly_the_domain_states() -> None:
    assert _check_values("shop_capabilities_state_check") == {s.value for s in CapabilityState}


def test_automation_executions_state_check__lists_exactly_the_execution_states() -> None:
    assert _check_values("automation_executions_state_check") == {s.value for s in ExecutionState}


def test_automation_executions_trigger_check__lists_exactly_the_trigger_kinds() -> None:
    assert _check_values("automation_executions_trigger_check") == {k.value for k in TriggerKind}


def test_automation_definitions_readiness_check__lists_exactly_the_readiness_values() -> None:
    assert _check_values("automation_definitions_readiness_check") == {r.value for r in Readiness}


def test_jobs_state_check__lists_exactly_the_job_states() -> None:
    assert _check_values("jobs_state_check") == {s.value for s in JobState}


def test_action_executions_state_check__lists_exactly_the_action_states() -> None:
    assert _check_values("action_executions_state_check") == {s.value for s in ActionState}


def test_action_executions_outcome_check__lists_exactly_the_action_outcomes() -> None:
    assert _check_values("action_executions_outcome_check") == {o.value for o in ActionOutcome}


def test_action_executions_channel_check__lists_exactly_the_channels() -> None:
    assert _check_values("action_executions_channel_check") == {c.value for c in Channel}


def test_circuit_breakers_state_check__lists_exactly_the_breaker_states() -> None:
    assert _check_values("circuit_breakers_state_check") == {s.value for s in BreakerState}
