"""Health endpoints: /healthz (process alive) and /readyz (dependencies ready), FR-AUD-03."""

import uuid

import anyio
import httpx
import pytest

from htco_api.app import create_app

PROBLEM_TYPES = "https://ht-co.app/problems/"


class _Check:
    def __init__(self, name: str, *, fails: bool = False, hangs: bool = False) -> None:
        self.name = name
        self.calls = 0
        self._fails = fails
        self._hangs = hangs

    async def __call__(self) -> None:
        self.calls += 1
        if self._hangs:
            await anyio.Event().wait()
        if self._fails:
            raise ConnectionError("password=hunter2 rejected by 10.0.0.5")


def _client_for(*checks: _Check, timeout: float = 1) -> httpx.AsyncClient:
    app = create_app(readiness_checks=checks, readiness_timeout_seconds=timeout)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.1")
async def test_healthz__get__returns_ok_with_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert uuid.UUID(response.headers["x-request-id"]).version == 7


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.1")
async def test_healthz__dependency_down__still_ok_and_never_calls_checks() -> None:
    database = _Check("database", fails=True)

    async with _client_for(database) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert database.calls == 0


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.2")
async def test_readyz__configuration_valid__returns_ready(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"configuration": "ok"}}


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.2")
async def test_readyz__all_checks_pass__lists_each_check() -> None:
    async with _client_for(_Check("database")) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"] == {"configuration": "ok", "database": "ok"}


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.2")
async def test_readyz__check_fails__returns_503_problem_without_internals() -> None:
    async with _client_for(_Check("database", fails=True)) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == PROBLEM_TYPES + "not-ready"
    assert body["status"] == 503
    assert body["checks"] == {"configuration": "ok", "database": "failed"}
    assert body["request_id"] == response.headers["x-request-id"]
    assert "hunter2" not in response.text
    assert "10.0.0.5" not in response.text


@pytest.mark.anyio
@pytest.mark.ac("AC-1.2.2")
async def test_readyz__check_hangs__fails_within_the_timeout() -> None:
    async with _client_for(_Check("database", hangs=True), timeout=0.01) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "failed"
