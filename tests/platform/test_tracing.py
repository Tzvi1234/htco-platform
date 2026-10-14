"""Trace context for logs and outgoing calls (SOFTWARE_DESIGN §12)."""

import re

from opentelemetry.sdk.trace import TracerProvider

from htco_platform.tracing import current_trace_id


def test_current_trace_id__outside_a_span__is_none() -> None:
    assert current_trace_id() is None


def test_current_trace_id__inside_a_span__is_its_32_hex_trace_id() -> None:
    tracer = TracerProvider().get_tracer("test")

    with tracer.start_as_current_span("work") as span:
        trace_id = current_trace_id()

    assert trace_id is not None
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
    assert int(trace_id, 16) == span.get_span_context().trace_id
