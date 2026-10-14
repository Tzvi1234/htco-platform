"""Every request gets a server-generated ID and one access log line, without query strings."""

import uuid

import httpx
import pytest

from tests.log_capture import LogCapture


@pytest.mark.anyio
async def test_request_id__each_request__gets_a_new_uuid7(client: httpx.AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    ids = {first.headers["x-request-id"], second.headers["x-request-id"]}
    assert len(ids) == 2
    assert all(uuid.UUID(value).version == 7 for value in ids)


@pytest.mark.anyio
async def test_request_id__client_supplied_header__is_not_trusted(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/healthz", headers={"X-Request-Id": "forged-id\r\nX-Evil: 1"})

    assert response.headers["x-request-id"] != "forged-id"
    assert uuid.UUID(response.headers["x-request-id"]).version == 7


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.6")
async def test_access_log__one_json_line_per_request_with_request_id(
    client: httpx.AsyncClient, logs: LogCapture
) -> None:
    response = await client.get("/healthz")

    [access] = [line for line in logs.lines() if line["event"] == "request.completed"]
    assert access["request_id"] == response.headers["x-request-id"]
    assert access["method"] == "GET"
    assert access["path"] == "/healthz"
    assert access["status"] == 200
    assert access["duration_ms"] >= 0
    assert {"timestamp", "level"} <= set(access)


@pytest.mark.anyio
async def test_access_log__query_string__is_never_logged(
    client: httpx.AsyncClient, logs: LogCapture
) -> None:
    await client.get("/healthz", params={"code": "oauth-code-secret"})

    assert "oauth-code-secret" not in logs.raw()


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.6")
async def test_access_log__error_response__records_its_status(
    client: httpx.AsyncClient, logs: LogCapture
) -> None:
    await client.get("/missing")

    [access] = [line for line in logs.lines() if line["event"] == "request.completed"]
    assert access["status"] == 404
