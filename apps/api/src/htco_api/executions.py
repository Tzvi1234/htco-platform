"""The chain view of one automation execution (slice 5.18).

`GET /v1/executions/{execution_id}` returns the execution's trigger, state changes, actions with
their channel and outcome, approvals, hand-offs to a person and published events, in causal
order. "View the office" decides; a shop-scoped role sees the executions of its granted shops
only, and any other execution does not exist for it (404).
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.identity import ORGANIZATION_REQUIRED, problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import NoMembershipError
from htco_application.shops import OrganizationRequiredError
from htco_application.status import (
    ChainReader,
    ExecutionChain,
    ExecutionNotVisibleError,
    execution_chain,
)

CHAIN_UNAVAILABLE = ProblemType(
    "executions-unavailable",
    "Executions not available",
    503,
    "Executions cannot be read on this server.",
)


class ChainEntryView(BaseModel):
    kind: str  # trigger, execution, action, approval, human_task, event
    id: str
    at: datetime
    name: str
    state: str | None
    status: str | None
    detail: dict[str, Any]


class ExecutionChainView(BaseModel):
    id: uuid.UUID
    automation_id: str
    shop_id: uuid.UUID | None
    state: str
    status: str
    correlation_id: str
    entries: list[ChainEntryView]
    # A succeeded run's handler output, checked against its contract (#127): per-run IDs and
    # counts, for the Owner and Admins; null otherwise.
    output: dict[str, Any] | None = None


def _reader(request: Request) -> ChainReader:
    found = getattr(request.app.state, "execution_chain", None)
    if found is None:
        raise ProblemError(CHAIN_UNAVAILABLE)
    return found  # type: ignore[no-any-return]


def _view(chain: ExecutionChain) -> ExecutionChainView:
    execution = chain.execution
    return ExecutionChainView(
        id=execution.id, automation_id=execution.automation_id, shop_id=execution.shop_id,
        state=execution.state.value, status=chain.status.value,
        correlation_id=execution.correlation_id,
        output=None if execution.output is None else dict(execution.output),
        entries=[
            ChainEntryView(
                kind=e.kind.value, id=e.id, at=e.at, name=e.name, state=e.state,
                status=None if e.status is None else e.status.value, detail=dict(e.detail),
            )
            for e in chain.entries
        ],
    )  # fmt: skip


def executions_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["automations"])

    @router.get(
        "/executions/{execution_id}",
        summary="One execution with its trigger, actions, approvals and events",
        responses=problem_responses(
            {
                400: "The caller belongs to several organizations and named none",
                403: "The role may not view the office (forbidden)",
                404: "No such execution for the caller",
                422: ("Invalid execution or organization ID", ValidationProblem),
                503: "Executions cannot be read on this server (executions-unavailable)",
            }
        ),
    )
    async def get_execution_chain(
        execution_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        reader: Annotated[ChainReader, Depends(_reader)],
        organization_id: uuid.UUID | None = None,
    ) -> ExecutionChainView:
        """In causal order; IDs, states and codes only, never payloads or buyer data. The run's
        checked output (per-run counts) is shown to the Owner and Admins."""
        try:
            chain = await anyio.to_thread.run_sync(
                lambda: execution_chain(
                    auth, reader, execution_id, organization_id=organization_id, now=clock()
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except PermissionError:
            raise ProblemError(FORBIDDEN) from None
        except ExecutionNotVisibleError, NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        response.headers["Cache-Control"] = "no-store"
        return _view(chain)

    return router
