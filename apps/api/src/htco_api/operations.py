"""Queue, execution and sync health for the Owner and Admins (slice 5.9).

`GET /v1/operations/health`: jobs by kind and state, dead letters with their last error, worker
heartbeats, the last reconciliation of each shop with its drifts, webhook intake counts, the
newest executions (each opens in `GET /v1/executions/{id}`), outbox delivery failures, and per
automation its executions, success rate, retries, dead letters and p50/p95 latency (7 days).
`POST /v1/operations/dead-letters/{job_id}/retry`: a dead job goes back to the queue, audited;
the engine's own jobs are retried through their execution instead (409 with its ID).

IDs, kinds, states, counts, times and error codes only: no payload and no buyer data.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Protocol

import anyio
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.idempotency import IDEMPOTENCY_OPENAPI, idempotency_store, run_idempotently
from htco_api.identity import ORGANIZATION_REQUIRED, problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import (
    NOT_FOUND,
    Problem,
    ProblemError,
    ProblemType,
    ValidationProblem,
    request_id_of,
)
from htco_application.access.authentication import NoMembershipError
from htco_application.idempotency import IdempotencyStore
from htco_application.operations_health import (
    DeadJobNotFoundError,
    DeadJobs,
    HealthReader,
    JobNotDeadError,
    OperationsForbiddenError,
    OperationsHealth,
    RetryThroughExecutionError,
    operations_health,
    retry_dead_job,
)
from htco_application.shops import OrganizationRequiredError

OPERATIONS_UNAVAILABLE = ProblemType(
    "operations-unavailable",
    "Operations health not available",
    503,
    "The queue and sync health cannot be read on this server.",
)
JOB_NOT_DEAD = ProblemType(
    "job-not-dead", "The job is not dead-lettered", 409, "Only a dead-lettered job is retried."
)
RETRY_THROUGH_EXECUTION = ProblemType(
    "retry-through-execution",
    "Retry the execution",
    409,
    "This job runs an automation execution: retry the execution instead.",
)


class RetryThroughExecutionProblem(Problem):
    execution_id: uuid.UUID | None


class OperationsPorts(HealthReader, DeadJobs, Protocol):
    """The Postgres adapter answers both."""


class JobCountView(BaseModel):
    kind: str
    state: str
    count: int


class DeadJobView(BaseModel):
    id: uuid.UUID
    kind: str
    shop_id: uuid.UUID | None
    attempts: int
    last_error: str | None
    finished_at: datetime
    execution_id: uuid.UUID | None


class WorkerView(BaseModel):
    worker_id: str
    release: str | None
    started_at: datetime
    last_beat_at: datetime
    age_seconds: int
    # False once the last beat is older than two leases, or the worker is stopping.
    alive: bool
    stopping: bool
    current_job_id: uuid.UUID | None


class ShopSyncView(BaseModel):
    shop_id: uuid.UUID
    resource: str
    kind: str
    last_started_at: datetime | None
    last_succeeded_at: datetime | None
    last_error: str | None
    drifts_last_day: int
    drifts_total: int
    # No success within the reconciliation window (13 hours).
    stale: bool


class WebhookCountView(BaseModel):
    source: str
    event_type: str
    received_last_day: int
    unprocessed: int


class ExecutionRowView(BaseModel):
    id: uuid.UUID
    automation_id: str
    shop_id: uuid.UUID | None
    state: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class OutboxFailureView(BaseModel):
    id: uuid.UUID
    destination: str
    status: str
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    action_execution_id: uuid.UUID | None


class AutomationMetricsView(BaseModel):
    automation_id: str
    executions: int
    succeeded: int
    dead_lettered: int
    retries: int
    # Succeeded among the finished ones, in percent; null when none finished.
    success_rate: Decimal | None
    p50_seconds: float | None
    p95_seconds: float | None


class OperationsHealthView(BaseModel):
    organization_id: uuid.UUID
    generated_at: datetime
    jobs: list[JobCountView]
    dead_letters: list[DeadJobView]
    workers: list[WorkerView]
    shops: list[ShopSyncView]
    webhooks: list[WebhookCountView]
    executions: list[ExecutionRowView]
    outbox_failures: list[OutboxFailureView]
    automations: list[AutomationMetricsView]


class RetriedJobView(BaseModel):
    job_id: uuid.UUID
    state: str


def _ports(request: Request) -> OperationsPorts:
    found = getattr(request.app.state, "operations", None)
    if found is None:
        raise ProblemError(OPERATIONS_UNAVAILABLE)
    return found  # type: ignore[no-any-return]


_Ports = Annotated[OperationsPorts, Depends(_ports)]


def _view(found: OperationsHealth) -> OperationsHealthView:
    return OperationsHealthView(
        organization_id=found.organization_id,
        generated_at=found.generated_at,
        jobs=[JobCountView(kind=j.kind, state=j.state, count=j.count) for j in found.jobs],
        dead_letters=[
            DeadJobView(
                id=d.id, kind=d.kind, shop_id=d.shop_id, attempts=d.attempts,
                last_error=d.last_error, finished_at=d.finished_at, execution_id=d.execution_id,
            )
            for d in found.dead_letters
        ],
        workers=[
            WorkerView(
                worker_id=w.beat.worker_id, release=w.beat.release, started_at=w.beat.started_at,
                last_beat_at=w.beat.last_beat_at, age_seconds=w.age_seconds, alive=w.alive,
                stopping=w.beat.stopping, current_job_id=w.beat.current_job_id,
            )
            for w in found.workers
        ],
        shops=[
            ShopSyncView(
                shop_id=s.sync.shop_id, resource=s.sync.resource, kind=s.sync.kind,
                last_started_at=s.sync.last_started_at,
                last_succeeded_at=s.sync.last_succeeded_at, last_error=s.sync.last_error,
                drifts_last_day=s.sync.drifts_last_day, drifts_total=s.sync.drifts_total,
                stale=s.stale,
            )
            for s in found.shops
        ],
        webhooks=[
            WebhookCountView(
                source=w.source, event_type=w.event_type,
                received_last_day=w.received_last_day, unprocessed=w.unprocessed,
            )
            for w in found.webhooks
        ],
        executions=[
            ExecutionRowView(
                id=e.id, automation_id=e.automation_id, shop_id=e.shop_id, state=e.state,
                attempts=e.attempts, last_error=e.last_error, created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in found.executions
        ],
        outbox_failures=[
            OutboxFailureView(
                id=o.id, destination=o.destination, status=o.status, attempts=o.attempts,
                last_error=o.last_error, next_attempt_at=o.next_attempt_at,
                action_execution_id=o.action_execution_id,
            )
            for o in found.outbox_failures
        ],
        automations=[
            AutomationMetricsView(
                automation_id=a.automation_id, executions=a.executions, succeeded=a.succeeded,
                dead_lettered=a.dead_lettered, retries=a.retries, success_rate=a.success_rate,
                p50_seconds=a.p50_seconds, p95_seconds=a.p95_seconds,
            )
            for a in found.automations
        ],
    )  # fmt: skip


_COMMON: dict[int, str | tuple[str, type[Problem]]] = {
    400: "The caller belongs to several organizations and named none",
    403: "Only the Owner and Admins see the queue (forbidden)",
    422: ("Invalid ID", ValidationProblem),
    503: "Not available on this server (operations-unavailable)",
}


def operations_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1/operations", tags=["operations"])

    @router.get(
        "/health",
        summary="Queue, execution and sync health",
        responses=problem_responses(_COMMON),
    )
    async def get_operations_health(
        response: Response,
        auth: CurrentAuth,
        ports: _Ports,
        organization_id: uuid.UUID | None = None,
    ) -> OperationsHealthView:
        """The Owner and Admins: IDs, states, counts, times and error codes, never payloads."""
        try:
            found = await anyio.to_thread.run_sync(
                lambda: operations_health(ports, auth, organization_id=organization_id, now=clock())
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except OperationsForbiddenError:
            raise ProblemError(FORBIDDEN) from None
        except NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        response.headers["Cache-Control"] = "no-store"
        return _view(found)

    @router.post(
        "/dead-letters/{job_id}/retry",
        status_code=202,
        summary="Send a dead-lettered job back to the queue (audited)",
        response_model=RetriedJobView,
        responses=problem_responses(
            {
                **_COMMON,
                400: "The caller belongs to several organizations and named none, or no or an "
                "invalid Idempotency-Key",
                404: "No such job in the organization",
                409: (
                    "The job is not dead (job-not-dead), or it runs an execution: retry the "
                    "execution (retry-through-execution, with `execution_id`), or the first "
                    "request with this Idempotency-Key still runs (idempotency-in-progress)",
                    RetryThroughExecutionProblem,
                ),
                422: (
                    "Invalid ID, or the Idempotency-Key was used with another request "
                    "(idempotency-key-reused)",
                    ValidationProblem,
                ),
            }
        ),
        openapi_extra=IDEMPOTENCY_OPENAPI,
    )
    async def retry_dead_letter(  # noqa: PLR0913, PLR0917 - the route's inputs
        job_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        ports: _Ports,
        idempotency: Annotated[IdempotencyStore, Depends(idempotency_store)],
        organization_id: uuid.UUID | None = None,
    ) -> JSONResponse:
        """Attempts start from zero and the job is due now; the audit records its kind and last
        error. Once per Idempotency-Key (audit 6.1)."""
        now = clock()

        async def work() -> tuple[int, dict[str, Any]]:
            try:
                await anyio.to_thread.run_sync(
                    lambda: retry_dead_job(
                        ports, auth, job_id, organization_id=organization_id,
                        correlation_id=request_id_of(request) or str(uuid.uuid7()), now=now,
                    )
                )  # fmt: skip
            except OrganizationRequiredError:
                raise ProblemError(ORGANIZATION_REQUIRED) from None
            except OperationsForbiddenError:
                raise ProblemError(FORBIDDEN) from None
            except DeadJobNotFoundError, NoMembershipError:
                raise ProblemError(NOT_FOUND) from None
            except JobNotDeadError:
                raise ProblemError(JOB_NOT_DEAD) from None
            except RetryThroughExecutionError as error:
                found = error.execution_id
                extra = {} if found is None else {"execution_id": str(found)}
                raise ProblemError(RETRY_THROUGH_EXECUTION, extra=extra) from None
            return 202, RetriedJobView(job_id=job_id, state="queued").model_dump(mode="json")

        return await run_idempotently(
            request, user_id=auth.user_id, store=idempotency, now=now, work=work
        )

    return router
