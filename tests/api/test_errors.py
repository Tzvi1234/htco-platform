"""Errors use one RFC 9457 envelope with a request_id and no internals (ER-CODE-06, ASVS 16.5)."""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from htco_api.app import create_app
from tests.log_capture import LogCapture

PROBLEM_TYPES = "https://ht-co.app/problems/"
PROBLEM_FIELDS = {"type", "title", "status", "detail", "request_id"}


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.3")
async def test_unknown_route__returns_404_problem_with_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/no/such/route")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert set(body) == PROBLEM_FIELDS
    assert body["type"] == PROBLEM_TYPES + "not-found"
    assert body["status"] == 404
    assert body["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text


@pytest.mark.anyio
async def test_wrong_method__returns_405_problem_and_keeps_the_allow_header(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/healthz")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["type"] == PROBLEM_TYPES + "method-not-allowed"


@pytest.mark.anyio
async def test_trace_method__is_not_supported(client: httpx.AsyncClient) -> None:
    response = await client.request("TRACE", "/healthz")

    assert response.status_code == 405


def _app_with_failing_route(app: FastAPI) -> FastAPI:
    async def explode() -> None:
        raise RuntimeError("database password is hunter2")

    app.add_api_route("/explode", explode)
    return app


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.4")
async def test_unhandled_exception__returns_generic_500_problem(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=_app_with_failing_route(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/explode")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert set(body) == PROBLEM_FIELDS
    assert body["type"] == PROBLEM_TYPES + "internal-error"
    assert body["request_id"] == response.headers["x-request-id"]
    for leak in ("hunter2", "RuntimeError", "Traceback", "explode"):
        assert leak not in response.text


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.4")
async def test_unhandled_exception__is_logged_with_stack_trace_and_the_same_request_id(
    app: FastAPI, logs: LogCapture
) -> None:
    transport = httpx.ASGITransport(app=_app_with_failing_route(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/explode")

    [failure] = [line for line in logs.lines() if line["event"] == "request.failed"]
    assert failure["request_id"] == response.headers["x-request-id"]
    assert failure["level"] == "error"
    [frame_group] = failure["exception"]
    assert frame_group["exc_type"] == "RuntimeError"
    assert frame_group["frames"]


@pytest.mark.anyio
async def test_exception_after_streaming_started__is_logged_and_the_connection_fails(
    app: FastAPI, logs: LogCapture
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"first chunk"
        raise RuntimeError("storage went away")

    async def stream() -> StreamingResponse:
        return StreamingResponse(chunks())

    app.add_api_route("/stream", stream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        with pytest.raises(RuntimeError, match="storage went away"):
            await client.get("/stream")

    events = [line["event"] for line in logs.lines()]
    assert events.count("request.failed") == 1
    assert "request.completed" in events


@pytest.mark.anyio
async def test_invalid_query_parameter__returns_422_problem_without_echoing_input(
    app: FastAPI,
) -> None:
    async def page(limit: Annotated[int, Query(ge=1, le=100)]) -> dict[str, int]:
        return {"limit": limit}

    app.add_api_route("/items", page)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/items", params={"limit": "secret-token-123"})

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == PROBLEM_TYPES + "validation-error"
    assert body["errors"] == [
        {
            "location": ["query", "limit"],
            "message": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]
    assert "secret-token-123" not in response.text


_PATH_SEGMENT = st.text(
    alphabet=st.characters(categories=("L", "N"), include_characters="-._~%!$&'()*+,;=:@"),
    min_size=1,
    max_size=40,
)


@pytest.fixture(scope="module")
def unknown_path_client() -> Iterator[TestClient]:
    """One app for every example: the requests are reads of unknown paths, so nothing carries
    over between them."""
    with TestClient(create_app()) as client:
        yield client


# Each example is one request (#115): building an app per example made this test take 45 s
# locally, which looked like a stall. The deadline keeps it that way.
@settings(max_examples=60, deadline=timedelta(milliseconds=500))
@given(segments=st.lists(_PATH_SEGMENT, min_size=1, max_size=4))
def test_any_unknown_path__is_a_404_problem_never_a_500(
    unknown_path_client: TestClient, segments: list[str]
) -> None:
    path = "/" + "/".join(segments)
    response = unknown_path_client.get(path)

    if response.status_code == 200:
        assert path in {"/healthz", "/readyz"}
        return
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert uuid.UUID(response.json()["request_id"]).version == 7
