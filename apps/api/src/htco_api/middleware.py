"""ASGI middleware: request context, last-resort error handling and security headers."""

import time
from collections.abc import Callable

import structlog
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from htco_api.problems import INTERNAL_ERROR, problem_response
from htco_platform.error_reporting import ErrorReporter
from htco_platform.tracing import current_trace_id

_logger = structlog.get_logger(__name__)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class RequestContextMiddleware:
    """Give every HTTP request an ID, a trace, one access log line and a last-resort handler.

    The request ID is always generated here; a client-supplied `X-Request-Id` is ignored, so it
    cannot forge or inject log correlation. The same goes for the trace: each request starts a
    new root span, whatever `traceparent` the client sent. The ID goes into `scope["state"]`,
    the logging context and the `X-Request-Id` response header. An unhandled exception is
    logged with its stack trace, sent to error monitoring and answered with a generic 500
    problem (ASVS 16.5.1, 16.5.4).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        new_request_id: Callable[[], str],
        tracer: Tracer,
        error_reporter: ErrorReporter,
    ) -> None:
        self._app = app
        self._new_request_id = new_request_id
        self._tracer = tracer
        self._error_reporter = error_reporter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = self._new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        response = {"started": False, "status": 500}

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response["started"], response["status"] = True, message["status"]
                MutableHeaders(scope=message)["X-Request-Id"] = request_id
            await send(message)

        span_attributes = {
            "http.request.method": scope["method"],
            "url.path": scope["path"],
            "htco.request_id": request_id,
        }
        with (
            self._tracer.start_as_current_span(
                scope["method"],
                context=Context(),
                kind=SpanKind.SERVER,
                attributes=span_attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span,
            structlog.contextvars.bound_contextvars(request_id=request_id),
        ):
            try:
                await self._app(scope, receive, send_with_request_id)
            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                _logger.exception("request.failed", method=scope["method"], path=scope["path"])
                self._report(error, request_id)
                if response["started"]:
                    raise
                failure = problem_response(INTERNAL_ERROR.problem(request_id))
                await failure(scope, receive, send_with_request_id)
            finally:
                span.set_attribute("http.response.status_code", response["status"])
                _logger.info(
                    "request.completed",
                    method=scope["method"],
                    path=scope["path"],
                    status=response["status"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )

    def _report(self, error: Exception, request_id: str) -> None:
        """Send the error to monitoring; a monitoring failure never changes the response."""
        try:
            self._error_reporter.report(error, request_id=request_id, trace_id=current_trace_id())
        except Exception as reporting_error:  # Monitoring must not break error handling.
            _logger.warning("error_reporting.failed", error_type=type(reporting_error).__name__)


class SecurityHeadersMiddleware:
    """Add the API security headers to every response unless a route set its own value."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self._app(scope, receive, send_with_headers)
