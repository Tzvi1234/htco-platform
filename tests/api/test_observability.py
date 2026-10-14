"""Tracing and error monitoring end to end through the API (AC-1.7.1, AC-1.7.2)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import override

import httpx
import pytest
import structlog
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from htco_api.app import AppOptions, create_app
from htco_platform.error_reporting import ErrorReporter, SentryErrorReporter
from htco_platform.http import new_async_http_client
from tests.log_capture import LogCapture
from tests.sentry_capture import FAKE_DSN, CapturingTransport

FORGED_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield client


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})


@pytest.mark.anyio
@pytest.mark.ac("AC-1.7.2")
async def test_request__one_trace_id_in_every_log_line_and_the_outgoing_call(
    logs: LogCapture,
) -> None:
    provider = TracerProvider()
    recorder = _Recorder()
    app = create_app(AppOptions(tracer_provider=provider))

    async def call_out() -> dict[str, bool]:
        transport = httpx.MockTransport(recorder)
        async with new_async_http_client(transport=transport, tracer_provider=provider) as client:
            await client.get("https://api.example/v3/ping")
        structlog.get_logger("htco.test").info("partner.called")
        return {"ok": True}

    app.add_api_route("/call-out", call_out)
    async with _client(app) as client:
        response = await client.get("/call-out")

    request_lines = [
        line for line in logs.lines() if line["request_id"] == response.headers["x-request-id"]
    ]
    assert {line["event"] for line in request_lines} >= {"partner.called", "request.completed"}
    [trace_id] = {line["trace_id"] for line in request_lines}
    assert trace_id is not None
    assert recorder.requests[0].headers["traceparent"].split("-")[1] == trace_id


@pytest.mark.anyio
async def test_request__incoming_traceparent__is_not_trusted(
    client: httpx.AsyncClient, logs: LogCapture
) -> None:
    forged = f"00-{FORGED_TRACE_ID}-b7ad6b7169203331-01"

    await client.get("/healthz", headers={"traceparent": forged})

    [access] = [line for line in logs.lines() if line["event"] == "request.completed"]
    assert access["trace_id"] is not None
    assert access["trace_id"] != FORGED_TRACE_ID


@pytest.mark.anyio
@pytest.mark.ac("AC-1.7.1")
async def test_diagnostic_error__is_reported_with_ids_and_without_request_secrets(
    logs: LogCapture,
) -> None:
    transport = CapturingTransport()
    reporter = SentryErrorReporter.from_dsn(
        FAKE_DSN, environment="test", release=None, transport=transport
    )
    app = create_app(AppOptions(diagnostics=True, error_reporter=reporter))

    async with _client(app) as client:
        response = await client.post(
            "/_diagnostics/error",
            content=b'{"card": "4111111111111111"}',
            headers={
                "Authorization": "Bearer secret-token-1",
                "Cookie": "__Host-session=secret-session-2",
                "Content-Type": "application/json",
            },
        )
    reporter.flush()

    assert response.status_code == 500
    [event] = transport.events
    [failure] = [line for line in logs.lines() if line["event"] == "request.failed"]
    assert event["tags"] == {
        "request_id": response.headers["x-request-id"],
        "trace_id": failure["trace_id"],
    }
    for secret in ("4111111111111111", "secret-token-1", "secret-session-2"):
        assert secret not in transport.serialized()


@pytest.mark.anyio
@pytest.mark.ac("AC-1.7.1")
async def test_diagnostics__off_by_default__the_route_does_not_exist(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/_diagnostics/error")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_error_reporter_failure__still_answers_with_the_500_problem(logs: LogCapture) -> None:
    class _BrokenReporter(ErrorReporter):
        @override
        def report(
            self, error: BaseException, *, request_id: str | None, trace_id: str | None
        ) -> None:
            raise ConnectionError("monitoring is down")

    app = create_app(AppOptions(diagnostics=True, error_reporter=_BrokenReporter()))

    async with _client(app) as client:
        response = await client.post("/_diagnostics/error")

    assert response.status_code == 500
    assert response.json()["type"].endswith("/internal-error")
    assert "error_reporting.failed" in [line["event"] for line in logs.lines()]
