"""Automation routes (slice 5.10, ADR-0013): the catalog, executions, user triggers, retries.

- `GET /v1/automations`: the current definitions ("View the office").
- `GET /v1/automations/{automation_id}/executions`: an organization's executions, newest first,
  cursor-paged; shop-scoped roles see their granted shops' executions (RLS).
- `POST /v1/automations/{automation_id}/executions`: a user action through the durable engine's
  single entry point, with an `Idempotency-Key` (required); 202 with the execution. The policy
  engine decides: 403 without the automation's capability, 404 for a shop the user may not use,
  409 when the engine refuses (not active, no handler deployed, not callable by users).
- `POST /v1/automations/{automation_id}/executions/{execution_id}/retry`: an Owner or Admin sends
  a dead-lettered execution back to its retry (audited); 409 when it is not dead-lettered.

Triggering and retrying are off unless `HTCO_AUTOMATIONS_ENABLED` is set (503 otherwise), so the
API stays deployable without a worker; reading needs only the database.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from htco_api.auth import CurrentAuth
from htco_api.idempotency import IDEMPOTENCY_HEADER, run_idempotently
from htco_api.identity import (
    ORGANIZATION_REQUIRED,
    decode_cursor,
    encode_cursor,
    problem_responses,
)
from htco_api.policy_guard import FORBIDDEN, MFA_REQUIRED, STEP_UP_REQUIRED
from htco_api.problems import NOT_FOUND, ProblemError, ProblemType, ValidationProblem, request_id_of
from htco_application.access.authentication import NoMembershipError
from htco_application.access.policy import Outcome
from htco_application.automation.durable import (
    DurableAutomationEngine,
    ExecutionNotFoundError,
    NotDeadLetteredError,
)
from htco_application.automation.engine import AutomationRefused
from htco_application.automation.records import ExecutionSummary
from htco_application.automation.requests import (
    AutomationForbiddenError,
    AutomationNotFoundError,
    ExecutionReader,
    ShopOwners,
    UserTrigger,
    list_definitions,
    list_executions,
    retry_as_admin,
    trigger_as_user,
)
from htco_application.idempotency import IdempotencyStore
from htco_application.shops import OrganizationRequiredError, ShopNotFoundError
from htco_domain.automation import AutomationDefinition, AutomationExecution

AUTOMATIONS_UNAVAILABLE = ProblemType(
    "automations-unavailable",
    "Automations unavailable",
    503,
    "Automations cannot be triggered on this server yet.",
)
AUTOMATION_REFUSED = ProblemType(
    "automation-refused",
    "Automation not available",
    409,
    "This automation cannot run now (not active, not deployed, or not started by users).",
)
NOT_DEAD_LETTERED = ProblemType(
    "execution-not-dead-lettered",
    "Nothing to retry",
    409,
    "Only a dead-lettered execution can be retried.",
)
_PROBLEM_FOR = {
    Outcome.DENY: FORBIDDEN,
    Outcome.MFA_REQUIRED: MFA_REQUIRED,
    Outcome.STEP_UP_REQUIRED: STEP_UP_REQUIRED,
}


@dataclass(frozen=True)
class AutomationServices:
    """What the routes need, built by the composition root."""

    definitions: Callable[[], Sequence[AutomationDefinition]]
    executions: ExecutionReader
    shops: ShopOwners
    idempotency: IdempotencyStore
    # None until HTCO_AUTOMATIONS_ENABLED: triggers and retries answer 503.
    engine: DurableAutomationEngine | None = None


class AutomationView(BaseModel):
    id: str
    name: str
    version: int
    business_owner: str
    primary_trigger: str
    callable_from: list[str]
    readiness: str
    activation_phase: int
    side_effect: str
    approval_required: bool
    # Whether a role may start it by hand (it names a capability).
    user_triggerable: bool


class AutomationList(BaseModel):
    items: list[AutomationView]


class ExecutionView(BaseModel):
    id: uuid.UUID
    automation_id: str
    automation_version: int
    shop_id: uuid.UUID | None
    trigger: str
    actor_kind: str
    actor_id: str
    state: str
    attempts: int
    correlation_id: str
    last_error: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ExecutionList(BaseModel):
    items: list[ExecutionView]
    next_cursor: str | None


class TriggerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None
    # IDs and parameters for the automation; never buyer contact data.
    payload: dict[str, Any] = Field(default_factory=dict, max_length=50)


def _services(request: Request) -> AutomationServices:
    services = getattr(request.app.state, "automations", None)
    if not isinstance(services, AutomationServices):
        raise ProblemError(AUTOMATIONS_UNAVAILABLE)
    return services


def _engine(services: AutomationServices) -> DurableAutomationEngine:
    if services.engine is None:
        raise ProblemError(AUTOMATIONS_UNAVAILABLE)
    return services.engine


def _view(definition: AutomationDefinition) -> AutomationView:
    return AutomationView(
        id=definition.id,
        name=definition.name,
        version=definition.version,
        business_owner=definition.business_owner,
        primary_trigger=definition.primary_trigger.value,
        callable_from=sorted(t.value for t in definition.callable_from),
        readiness=definition.readiness.value,
        activation_phase=definition.activation_phase,
        side_effect=definition.side_effect.value,
        approval_required=definition.approval_required,
        user_triggerable=definition.user_capability is not None,
    )


def _execution_view(execution: AutomationExecution | ExecutionSummary) -> ExecutionView:
    times: dict[str, datetime] = {}
    if isinstance(execution, ExecutionSummary):
        times = {"created_at": execution.created_at, "updated_at": execution.updated_at}
    return ExecutionView(
        id=execution.id,
        automation_id=execution.automation_id,
        automation_version=execution.automation_version,
        shop_id=execution.shop_id,
        trigger=execution.trigger.value,
        actor_kind=execution.actor.kind.value,
        actor_id=execution.actor.id,
        state=execution.state.value,
        attempts=execution.attempts,
        correlation_id=execution.correlation_id,
        last_error=execution.last_error,
        **times,
    )


def _refusal(error: Exception) -> ProblemError:
    """The problem for a use case's refusal."""
    if isinstance(error, OrganizationRequiredError):
        return ProblemError(ORGANIZATION_REQUIRED)
    if isinstance(error, AutomationForbiddenError):
        return ProblemError(_PROBLEM_FOR.get(error.outcome, FORBIDDEN))
    if isinstance(error, NotDeadLetteredError):
        return ProblemError(NOT_DEAD_LETTERED)
    if isinstance(error, AutomationRefused):
        return ProblemError(AUTOMATION_REFUSED)
    return ProblemError(NOT_FOUND)


_REFUSALS = (
    OrganizationRequiredError,
    AutomationForbiddenError,
    NotDeadLetteredError,
    AutomationRefused,
    NoMembershipError,
    AutomationNotFoundError,
    ShopNotFoundError,
    ExecutionNotFoundError,
)
_Services = Annotated[AutomationServices, Depends(_services)]


def automations_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["automations"])

    @router.get(
        "/automations",
        summary="The office's automations",
        responses=problem_responses(
            {
                400: "The caller belongs to several organizations and named none",
                403: "The role may not view the office (forbidden)",
                404: "The named organization does not exist for the caller",
                422: ("Invalid organization ID", ValidationProblem),
                503: "Automations are not set up on this server (automations-unavailable)",
            }
        ),
    )
    async def list_automations(
        auth: CurrentAuth, services: _Services, organization_id: uuid.UUID | None = None
    ) -> AutomationList:
        """The current version of every automation, from the reviewed registry."""
        definitions = await anyio.to_thread.run_sync(services.definitions)
        try:
            found = list_definitions(
                auth, definitions, organization_id=organization_id, now=clock()
            )
        except _REFUSALS as error:
            raise _refusal(error) from None
        return AutomationList(items=[_view(d) for d in found])

    @router.get(
        "/automations/{automation_id}/executions",
        summary="Executions of an automation in an organization",
        responses=problem_responses(
            {
                400: "The caller belongs to several organizations and named none",
                403: "The role may not view the office (forbidden)",
                404: "The named organization does not exist for the caller",
                422: ("Invalid organization ID, limit or cursor", ValidationProblem),
                503: "Automations are not set up on this server (automations-unavailable)",
            }
        ),
    )
    async def list_automation_executions(  # noqa: PLR0913, PLR0917 - path, filters and page
        automation_id: Annotated[str, Path(max_length=64)],
        auth: CurrentAuth,
        services: _Services,
        organization_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=256)] = None,
    ) -> ExecutionList:
        """Newest first; pass `next_cursor` back as `cursor` for the next page."""
        before = decode_cursor(cursor) if cursor is not None else None
        try:
            page = await list_executions(
                auth,
                services.executions,
                automation_id=automation_id,
                organization_id=organization_id,
                before=before,
                limit=limit,
                now=clock(),
            )
        except _REFUSALS as error:
            raise _refusal(error) from None
        return ExecutionList(
            items=[_execution_view(e) for e in page.items],
            next_cursor=None if page.next_before is None else encode_cursor(page.next_before),
        )

    @router.post(
        "/automations/{automation_id}/executions",
        summary="Start an automation (a user action)",
        status_code=202,
        response_model=ExecutionView,
        responses=problem_responses(
            {
                400: "No or an invalid Idempotency-Key, or several organizations and none named",
                403: "The role lacks the automation's capability (forbidden, mfa-required, "
                "step-up-required)",
                404: "No such automation or shop for the caller",
                409: "The automation cannot run (automation-refused), or the first request with "
                "this Idempotency-Key still runs (idempotency-in-progress)",
                422: (
                    "Invalid body, or the Idempotency-Key was used with another request "
                    "(idempotency-key-reused)",
                    ValidationProblem,
                ),
                503: "Automations are off on this server (automations-unavailable)",
            }
        ),
        openapi_extra={
            "parameters": [
                {
                    "name": IDEMPOTENCY_HEADER,
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                    "description": "A key for this request; a retry with the same key and body "
                    "returns the first response (24 hours).",
                }
            ]
        },
    )
    async def start_automation_execution(
        automation_id: Annotated[str, Path(max_length=64)],
        body: TriggerBody,
        request: Request,
        auth: CurrentAuth,
        services: _Services,
    ) -> JSONResponse:
        """Creates the execution once per Idempotency-Key; the worker runs it."""
        engine = _engine(services)
        now = clock()
        trigger = UserTrigger(
            automation_id=automation_id,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            payload=body.payload,
            correlation_id=request_id_of(request) or str(uuid.uuid7()),
            organization_id=body.organization_id,
            shop_id=body.shop_id,
        )

        async def work() -> tuple[int, dict[str, Any]]:
            try:
                execution = await anyio.to_thread.run_sync(
                    lambda: trigger_as_user(engine, services.shops, auth, trigger, now=now)
                )
            except _REFUSALS as error:
                raise _refusal(error) from None
            return 202, _execution_view(execution).model_dump(mode="json")

        return await run_idempotently(
            request, user_id=auth.user_id, store=services.idempotency, now=now, work=work
        )

    @router.post(
        "/automations/{automation_id}/executions/{execution_id}/retry",
        summary="Retry a dead-lettered execution",
        status_code=202,
        responses=problem_responses(
            {
                400: "The caller belongs to several organizations and named none",
                403: "Only the Owner and Admins retry (forbidden)",
                404: "No such execution for the caller",
                409: "The execution is not dead-lettered (execution-not-dead-lettered)",
                422: ("Invalid IDs", ValidationProblem),
                503: "Automations are off on this server (automations-unavailable)",
            }
        ),
    )
    async def retry_automation_execution(  # noqa: PLR0913, PLR0917 - path, query and context
        automation_id: Annotated[str, Path(max_length=64)],
        execution_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        services: _Services,
        organization_id: uuid.UUID | None = None,
    ) -> ExecutionView:
        """The same execution goes back to its retry with a new job; audited."""
        engine = _engine(services)
        now = clock()
        correlation = request_id_of(request) or str(uuid.uuid7())
        try:
            retried = await anyio.to_thread.run_sync(
                lambda: retry_as_admin(
                    engine,
                    auth,
                    organization_id=organization_id,
                    automation_id=automation_id,
                    execution_id=execution_id,
                    correlation_id=correlation,
                    now=now,
                )
            )
        except _REFUSALS as error:
            raise _refusal(error) from None
        return _execution_view(retried)

    return router
