"""Fixtures for API tests: the app under test, an HTTP client and captured logs."""

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI

from htco_api.app import create_app
from tests.log_capture import LogCapture, capture_logs


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield client


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    with capture_logs() as captured:
        yield captured
