"""Security headers on every API response (ASVS 5.0 V3.4, OWASP REST guidance)."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

EXPECTED_HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
    "referrer-policy": "no-referrer",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/missing"])
async def test_security_headers__every_response__has_them(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)

    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value, name


@pytest.mark.anyio
async def test_security_headers__server_error__has_them(app: FastAPI) -> None:
    async def explode() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/explode", explode)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/explode")

    assert response.status_code == 500
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_security_headers__route_sets_its_own_cache_policy__is_kept(app: FastAPI) -> None:
    async def public() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={"Cache-Control": "public, max-age=60"})

    app.add_api_route("/public", public)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/public")

    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.anyio
async def test_documentation_endpoints__are_not_exposed(client: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404, path
