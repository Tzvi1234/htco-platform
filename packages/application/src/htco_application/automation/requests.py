"""User actions on automations (slice 5.10): list, read executions, trigger, retry.

The policy engine decides, never the route (ER-SEC-03). Listing needs `view_agent_directory`
("View the office"). A user may trigger an automation only when its definition names a
`user_capability` and the user's role holds it (403 otherwise, AC-5.10.3); a shop that is not the
organization's, or not granted to the user, does not exist for them (404). Retrying a
dead-lettered execution is an organization-wide `retry_sync` (Owner, Admin), audited.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import (
    Capability,
    Decision,
    Outcome,
    Principal,
    Scope,
    ShopRef,
    decide,
)
from htco_application.automation.durable import DurableAutomationEngine
from htco_application.automation.engine import TriggerRequest
from htco_application.automation.records import ExecutionPage
from htco_application.kernel import ShopOwners
from htco_application.shops import OrganizationRequiredError, ShopNotFoundError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationDefinition, AutomationExecution, TriggerKind

MAX_PAGE = 100


class AutomationNotFoundError(Exception):
    pass


class AutomationForbiddenError(Exception):
    def __init__(self, outcome: Outcome) -> None:
        super().__init__(f"not allowed ({outcome.value})")
        self.outcome = outcome


class ExecutionReader(Protocol):
    async def list_executions(
        self,
        claims: Mapping[str, Any],
        organization_id: uuid.UUID,
        automation_id: str,
        *,
        before: uuid.UUID | None,
        limit: int,
    ) -> ExecutionPage: ...


@dataclass(frozen=True)
class UserTrigger:
    automation_id: str
    idempotency_key: str  # the request's Idempotency-Key
    payload: Mapping[str, object]
    correlation_id: str
    organization_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None


def _principal(auth: AuthContext, organization_id: uuid.UUID | None) -> Principal:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    return auth.principal_for(organization)


def _allowed(decision: Decision) -> None:
    if decision.outcome is not Outcome.ALLOW:
        raise AutomationForbiddenError(decision.outcome)


def list_definitions(
    auth: AuthContext,
    definitions: Sequence[AutomationDefinition],
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
) -> list[AutomationDefinition]:
    principal = _principal(auth, organization_id)
    _allowed(decide(principal, Capability.VIEW_AGENT_DIRECTORY, shop=None, now=now))
    return sorted(definitions, key=lambda d: d.id)


async def list_executions(  # noqa: PLR0913 - the caller, the automation and the page
    auth: AuthContext,
    reader: ExecutionReader,
    *,
    automation_id: str,
    organization_id: uuid.UUID | None,
    before: uuid.UUID | None,
    limit: int,
    now: datetime,
) -> ExecutionPage:
    """Shop-scoped roles see the executions of their granted shops (RLS decides the rows)."""
    principal = _principal(auth, organization_id)
    _allowed(decide(principal, Capability.VIEW_AGENT_DIRECTORY, shop=None, now=now))
    return await reader.list_executions(
        auth.claims,
        principal.organization_id,
        automation_id,
        before=before,
        limit=max(1, min(limit, MAX_PAGE)),
    )


def trigger_as_user(
    engine: DurableAutomationEngine,
    shops: ShopOwners,
    auth: AuthContext,
    request: UserTrigger,
    *,
    now: datetime,
) -> AutomationExecution:
    """Raises `AutomationNotFoundError`, `AutomationForbiddenError`, `ShopNotFoundError`,
    `NoMembershipError`, `OrganizationRequiredError`, or the engine's `AutomationRefused`."""
    principal = _principal(auth, request.organization_id)
    definition = engine.definition(request.automation_id)
    if definition is None:
        raise AutomationNotFoundError(request.automation_id)
    if definition.user_capability is None:
        raise AutomationForbiddenError(Outcome.DENY)
    capability = Capability(definition.user_capability)
    _allowed(decide(principal, capability, shop=None, now=now))
    if request.shop_id is not None:
        owner = shops.organization_of_shop(request.shop_id)
        if owner != principal.organization_id:
            raise ShopNotFoundError("no such shop")
        shop = ShopRef(id=request.shop_id, organization_id=owner)
        if decide(principal, capability, shop=shop, now=now).outcome is not Outcome.ALLOW:
            raise ShopNotFoundError("no such shop")
    return engine.trigger(
        TriggerRequest(
            automation_id=definition.id,
            trigger=TriggerKind.USER_ACTION,
            actor=ActorRef(ActorKind.USER, str(auth.user_id)),
            # Per organization, so one tenant's key never meets another's execution.
            idempotency_key=f"user:{principal.organization_id}:{request.idempotency_key}",
            payload=request.payload,
            correlation_id=request.correlation_id,
            organization_id=principal.organization_id,
            shop_id=request.shop_id,
        )
    )


def retry_as_admin(  # noqa: PLR0913 - the caller and the execution
    engine: DurableAutomationEngine,
    auth: AuthContext,
    *,
    organization_id: uuid.UUID | None,
    automation_id: str,
    execution_id: uuid.UUID,
    correlation_id: str,
    now: datetime,
) -> AutomationExecution:
    principal = _principal(auth, organization_id)
    decision = decide(principal, Capability.RETRY_SYNC, shop=None, now=now)
    _allowed(decision)
    if decision.scope is not Scope.ORGANIZATION:
        raise AutomationForbiddenError(Outcome.DENY)
    return engine.retry_dead_lettered(
        principal.organization_id,
        execution_id,
        actor=ActorRef(ActorKind.USER, str(auth.user_id)),
        correlation_id=correlation_id,
        automation_id=automation_id,
    )
