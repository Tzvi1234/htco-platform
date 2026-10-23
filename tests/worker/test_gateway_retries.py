"""One retry layer on the worker's gateway path (audit P1-2): the Action Gateway's guards (5.15)
own retries and defers, and the Etsy adapter behind the API channel fails fast. Before, the
adapter's four attempts ran inside each of the gateway's three, up to 12 HTTP calls per read,
and its 60 s wait on a 429 overrode the gateway's 5 s defer rule."""

import uuid
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import ActionOutcome
from htco_application.execution.guards import InlineRetry
from htco_integrations.etsy import EtsyAdapter, EtsyConfig, EtsyCredentials, Timekeeping
from htco_persistence.database import Database
from htco_platform.http import new_async_http_client
from htco_worker.main import etsy_parts, gateway_etsy_config
from htco_worker.settings import WorkerSettings
from tests.application.test_action_gateway import ask
from tests.application.test_action_guards import Clock, gateway, guards
from tests.application.test_execution_router import EtsyShops
from tests.integrations.test_etsy_adapter import FakeTime, FakeTokens, Scripted, quota_headers

pytestmark = pytest.mark.anyio

DATABASE = "postgresql://htco@db.invalid/htco"
VAULT_KEY = "a" * 64


def _api_channel(transport: Scripted, time: FakeTime) -> EtsyApiChannel:
    adapter = EtsyAdapter(
        new_async_http_client(
            tracer_provider=TracerProvider(), transport=httpx.MockTransport(transport)
        ),
        credentials=EtsyCredentials(keystring="fakekeystring", shared_secret="fake-secret"),
        access_tokens=FakeTokens(),
        config=gateway_etsy_config(daily_quota_floor=0),
        timekeeping=Timekeeping(now=time.now, monotonic=time.monotonic, sleep=time.sleep),
    )
    return EtsyApiChannel(adapter, EtsyShops())


def _response(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": "x"}, headers=quota_headers() | headers)


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"count": 0, "results": []}, headers=quota_headers())


async def _submit(transport: Scripted) -> tuple[Any, FakeTime, Clock]:
    time, clock = FakeTime(), Clock()
    service = gateway(_api_channel(transport, time), clock, guards(clock))  # type: ignore[arg-type]
    result = await service.submit(ask(key=f"read:{uuid.uuid4()}"))
    return result, time, clock


# --- the configuration -----------------------------------------------------------------------


def test_gateway_config__one_attempt_and_no_waiting_in_the_adapter() -> None:
    config = gateway_etsy_config(daily_quota_floor=250)

    assert (config.max_attempts, config.max_retry_after_seconds) == (1, 0)
    assert config.daily_quota_floor == 250


def test_worker__the_gateway_adapter_fails_fast__the_probes_keep_their_retries() -> None:
    settings = WorkerSettings.model_validate(
        {
            "environment": "test",
            "database_url": DATABASE,
            "worker_health_port": 0,
            "token_vault_key": VAULT_KEY,
            "etsy_keystring": "fake-keystring",
            "etsy_shared_secret": "fake-secret",
        }
    )

    parts = etsy_parts(settings, Database(DATABASE), httpx.AsyncClient())

    assert parts.gateway_adapter is not None
    assert parts.adapter is not None
    assert parts.gateway_adapter.config.max_attempts == 1
    assert parts.adapter.config.max_attempts == EtsyConfig().max_attempts


# --- the bound -------------------------------------------------------------------------------


async def test_a_read_against_a_failing_etsy__at_most_the_gateways_attempts() -> None:
    transport = Scripted(_response(503))

    result, time, _ = await _submit(transport)

    assert result.outcome is ActionOutcome.FAILED
    assert len(transport.requests) == InlineRetry().max_attempts == 3
    assert time.sleeps == []  # the adapter never waits; the gateway's backoff does


async def test_a_read_against_a_dropping_network__at_most_the_gateways_attempts() -> None:
    transport = Scripted(httpx.ConnectError("refused"))

    result, _, _ = await _submit(transport)

    assert result.outcome is ActionOutcome.FAILED
    assert len(transport.requests) <= InlineRetry().max_attempts


async def test_a_429_with_a_long_wait__one_call_then_deferred_by_the_gateway() -> None:
    transport = Scripted(_response(429, **{"retry-after": "30"}))

    result, time, clock = await _submit(transport)

    assert result.outcome is ActionOutcome.DEFERRED
    assert len(transport.requests) == 1
    assert time.sleeps == []
    assert clock.slept == []


async def test_a_429_with_a_short_wait__the_gateway_waits_once_and_the_retry_succeeds() -> None:
    transport = Scripted(_response(429, **{"retry-after": "2"}), _ok())

    result, time, clock = await _submit(transport)

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert len(transport.requests) == 2
    assert time.sleeps == []  # not the adapter's wait
    assert clock.slept == [2.0]  # the gateway's
