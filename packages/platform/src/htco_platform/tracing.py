"""Trace context for logs, error reports and outgoing calls (SOFTWARE_DESIGN §12).

The API starts a new trace for every request and ignores incoming `traceparent` headers, just
as it ignores client-supplied request IDs; clients from `htco_platform.http` carry the trace to
the services we call. Spans are not exported yet: the exporter joins when a tracing backend is
chosen, without changing this module's callers.
"""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider


def new_tracer_provider(*, service_name: str, environment: str) -> TracerProvider:
    return TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment.name": environment}
        )
    )


def current_trace_ids() -> tuple[str, str] | None:
    """`(trace_id, span_id)` of the active span as lowercase hex, or None outside a trace."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def current_trace_id() -> str | None:
    ids = current_trace_ids()
    return ids[0] if ids else None
