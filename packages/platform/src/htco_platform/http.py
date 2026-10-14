"""Clients for outgoing HTTP calls (ER-REL-03, ASVS 15.3.2).

Every client has a timeout, never follows redirects silently, and carries the current trace to
the callee as a W3C `traceparent` header. Adapters create their clients here, never with
`httpx` directly.
"""

import httpx
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.trace import TracerProvider

DEFAULT_TIMEOUT_SECONDS = 10.0


def new_async_http_client(
    *,
    tracer_provider: TracerProvider,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> httpx.AsyncClient:
    inner = transport if transport is not None else httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        transport=AsyncOpenTelemetryTransport(inner, tracer_provider=tracer_provider),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )
