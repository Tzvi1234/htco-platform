"""Health endpoints for the platform's probes (FR-AUD-03, ER-OPS-01).

`/healthz` answers whether the process is alive and touches nothing else. `/readyz` answers
whether every dependency is usable; configuration is always "ok" because the process only
starts with valid settings.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from htco_api.problems import (
    NOT_READY,
    PROBLEM_MEDIA_TYPE,
    Problem,
    problem_response,
    request_id_of,
)
from htco_platform.readiness import ReadinessCheck, run_readiness_checks


class HealthStatus(BaseModel):
    status: Literal["ok"]


class CheckStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


class ReadinessStatus(BaseModel):
    status: Literal["ready"]
    checks: dict[str, CheckStatus]


class NotReadyProblem(Problem):
    checks: dict[str, CheckStatus]


def health_router(
    *, readiness_checks: Sequence[ReadinessCheck], timeout_seconds: float
) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz", summary="The process is alive")
    async def healthz() -> HealthStatus:
        """Liveness: answers without touching any dependency."""
        return HealthStatus(status="ok")

    @router.get(
        "/readyz",
        summary="Every dependency is ready",
        response_model=ReadinessStatus,
        responses={
            503: {
                "model": NotReadyProblem,
                "content": {PROBLEM_MEDIA_TYPE: {}},
                "description": "A dependency is not ready",
            }
        },
    )
    async def readyz(request: Request) -> Response:
        """Readiness: runs every dependency check with a timeout."""
        results = await run_readiness_checks(readiness_checks, timeout_seconds=timeout_seconds)
        checks = {"configuration": CheckStatus.OK} | {
            result.name: CheckStatus.OK if result.ok else CheckStatus.FAILED for result in results
        }
        if all(status is CheckStatus.OK for status in checks.values()):
            return JSONResponse(
                ReadinessStatus(status="ready", checks=checks).model_dump(mode="json")
            )
        base = NOT_READY.problem(request_id_of(request))
        return problem_response(NotReadyProblem(**base.model_dump(), checks=checks))

    return router
