"""Outgoing HTTP: always a timeout, never a silent redirect, trace context propagated."""

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_platform.http import DEFAULT_TIMEOUT_SECONDS, new_async_http_client


class _Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response or httpx.Response(200, json={"ok": True})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


@pytest.mark.anyio
async def test_client__has_a_timeout_by_default() -> None:
    transport = httpx.MockTransport(_Recorder())
    async with new_async_http_client(
        transport=transport, tracer_provider=TracerProvider()
    ) as client:
        assert client.timeout == httpx.Timeout(DEFAULT_TIMEOUT_SECONDS)


@pytest.mark.anyio
async def test_client__redirect__is_returned_not_followed() -> None:
    redirect = httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    recorder = _Recorder(redirect)

    transport = httpx.MockTransport(recorder)
    async with new_async_http_client(
        transport=transport, tracer_provider=TracerProvider()
    ) as client:
        response = await client.get("https://api.example/v3/shops")

    assert response.status_code == 302
    assert len(recorder.requests) == 1


@pytest.mark.anyio
@pytest.mark.ac("AC-1.7.2")
async def test_client__inside_a_span__propagates_the_trace_as_traceparent() -> None:
    provider = TracerProvider()
    recorder = _Recorder()
    transport = httpx.MockTransport(recorder)

    with provider.get_tracer("test").start_as_current_span("request") as span:
        async with new_async_http_client(transport=transport, tracer_provider=provider) as client:
            await client.get("https://api.example/v3/shops")

    traceparent = recorder.requests[0].headers["traceparent"]
    assert traceparent.split("-")[1] == format(span.get_span_context().trace_id, "032x")
