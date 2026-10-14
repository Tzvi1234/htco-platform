"""The FastAPI application factory."""

import uuid
from collections.abc import Callable, Sequence

from fastapi import FastAPI

from htco_api.health import health_router
from htco_api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from htco_api.problems import add_problem_handlers
from htco_platform.readiness import ReadinessCheck


def _new_request_id() -> str:
    return str(uuid.uuid7())


def create_app(
    *,
    readiness_checks: Sequence[ReadinessCheck] = (),
    readiness_timeout_seconds: float = 2.0,
    new_request_id: Callable[[], str] = _new_request_id,
) -> FastAPI:
    """Build the API. Interactive docs and `/openapi.json` are off: the contract is the
    committed `packages/contracts/openapi.yaml` (ASVS 13.4.5)."""
    app = FastAPI(
        title="HTCO API",
        summary="Backend API of HTCO OS.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(
        health_router(readiness_checks=readiness_checks, timeout_seconds=readiness_timeout_seconds)
    )
    add_problem_handlers(app)
    # The last middleware added runs first: security headers wrap everything, including the
    # 500 response the request-context middleware produces.
    app.add_middleware(RequestContextMiddleware, new_request_id=new_request_id)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
