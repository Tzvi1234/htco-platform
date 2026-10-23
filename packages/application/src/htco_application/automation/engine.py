"""The workflow execution engine of the Automation Department (ADR-0013).

Every trigger (an external event, a schedule, an authorized agent tool, a user action or
another workflow) enters through `trigger`, which checks the automation's readiness, the
trigger kind and, for employees, the employee's automation grants. An execution is keyed by
`(automation_id, idempotency_key)`: a replayed trigger returns the existing execution and never
runs the business action again. Failures follow the automation's retry policy and end in the
dead-letter state.

This module keeps what every engine shares: the trigger request, the refusal and `authorize`.
The engine itself is `htco_application.automation.durable.DurableAutomationEngine` (slice 5.10);
the first in-memory engine was retired with slice 5.11 (audit P2-5).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import (
    AutomationDefinition,
    TriggerKind,
)
from htco_domain.workforce import PRODUCTION_READINESS


class AutomationRefused(Exception):  # noqa: N818 - reads as "automation (was) refused"
    pass


@dataclass(frozen=True)
class TriggerRequest:
    automation_id: str
    trigger: TriggerKind
    actor: ActorRef
    idempotency_key: str
    payload: Mapping[str, object]
    correlation_id: str
    # The tenant (durable engine, slice 5.10) and what caused the trigger.
    organization_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None
    causation_id: str | None = None


def authorize(
    registry: Registry, handlers: Mapping[str, object], request: TriggerRequest
) -> AutomationDefinition:
    """The single gate for every trigger (ADR-0013): known, production-ready, deployed, callable
    from this trigger kind, and for employees granted. Raises `AutomationRefused` with the reason,
    before anything is written (AC-5.10.2)."""
    definition = registry.automations.get(request.automation_id)
    if definition is None:
        raise AutomationRefused(f"unknown automation {request.automation_id!r}")
    if definition.readiness not in PRODUCTION_READINESS:
        raise AutomationRefused(f"{definition.id} is not active ({definition.readiness.value})")
    if definition.id not in handlers:
        raise AutomationRefused(f"{definition.id} has no handler deployed")
    if request.trigger not in definition.callable_from:
        raise AutomationRefused(f"{definition.id} cannot be triggered by {request.trigger.value}")
    if request.actor.kind is ActorKind.EMPLOYEE:
        _authorize_employee(registry, definition, request)
    return definition


def _authorize_employee(
    registry: Registry, definition: AutomationDefinition, request: TriggerRequest
) -> None:
    employee = request.actor.id
    if request.trigger is not TriggerKind.AGENT_TOOL:
        raise AutomationRefused("employees trigger automations only through agent_tool")
    profile = registry.employees.get(employee)
    if profile is None:
        raise AutomationRefused(f"unknown employee {employee!r}")
    if not profile.is_production_ready():
        raise AutomationRefused(f"{employee} is not active ({profile.readiness.value})")
    if definition.id not in profile.allowed_automations:
        raise AutomationRefused(f"{definition.id} is not granted to {employee}")
