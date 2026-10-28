"""AC-6.7.4 in the worker: a dead-lettered execution reaches Sentry through the worker's
`ErrorReporter` (none without a DSN), and the event holds identifiers only, never buyer data."""

import os
from datetime import timedelta

import pytest

from htco_application.automation.durable import (
    DurableAutomationEngine,
    HandlerContext,
    HandlerOutcome,
)
from htco_application.execution.gateway import UnavailableGateway
from htco_domain.automation import ExecutionState
from htco_platform.error_reporting import NoErrorReporter, SentryErrorReporter
from htco_platform.settings import load_settings
from htco_worker.error_reports import dead_letter_reporter, worker_error_reporter
from htco_worker.settings import WorkerSettings
from tests.application.test_durable_engine import NOW, WORKER, paid, registry
from tests.automation_fakes import MemoryExecutionStore
from tests.sentry_capture import FAKE_DSN, CapturingTransport

pytestmark = pytest.mark.anyio

BUYER_EMAIL = "jane.buyer@example.com"
BUYER_NAME = "Jane Buyer"
BUYER_ADDRESS = "12 Elm Street, Springfield"


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_ENVIRONMENT", "test")
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    return monkeypatch


@pytest.mark.ac("AC-6.7.4")
@pytest.mark.parametrize("dsn", [None, "", "   "])
def test_worker_error_reporter__without_a_dsn__reports_nothing(
    environment: pytest.MonkeyPatch, dsn: str | None
) -> None:
    if dsn is not None:
        environment.setenv("HTCO_SENTRY_DSN", dsn)

    assert isinstance(worker_error_reporter(load_settings(WorkerSettings)), NoErrorReporter)


@pytest.mark.ac("AC-6.7.4")
def test_worker_error_reporter__with_a_dsn__is_sentry(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("HTCO_SENTRY_DSN", FAKE_DSN)

    reporter = worker_error_reporter(load_settings(WorkerSettings))
    try:
        assert isinstance(reporter, SentryErrorReporter)
    finally:
        reporter.close()


@pytest.mark.ac("AC-6.7.4")
async def test_dead_letter__one_sentry_event_with_identifiers_and_no_buyer_data() -> None:
    transport = CapturingTransport()
    sentry = SentryErrorReporter.from_dsn(
        FAKE_DSN, environment="test", release="abc123", transport=transport
    )

    async def leaky(context: HandlerContext) -> HandlerOutcome:  # noqa: ARG001
        raise ValueError(f"no shipment for {BUYER_NAME} <{BUYER_EMAIL}> at {BUYER_ADDRESS}")

    now = NOW
    store = MemoryExecutionStore()
    engine = DurableAutomationEngine(
        registry,
        store,
        {"etsy-order-intake": leaky},
        clock=lambda: now,
        jitter=lambda: 1.0,
        gateway=UnavailableGateway(),
        on_dead_letter=dead_letter_reporter(sentry),
    )
    execution = engine.trigger(
        paid(payload={"receipt_id": "3412987711", "buyer_email": BUYER_EMAIL})
    )
    await engine.execute(execution.id, worker=WORKER)
    now += timedelta(seconds=10)
    done = await engine.execute(execution.id, worker=WORKER)
    sentry.flush()

    assert done.state is ExecutionState.DEAD_LETTERED
    [event] = transport.events
    [exception] = event["exception"]["values"]
    assert exception["type"] == "DeadLetteredExecutionError"
    assert exception["value"] == (
        f"automation.dead_lettered: etsy-order-intake execution={execution.id} "
        "error_type=ValueError error=ValueError"
    )
    sent = transport.serialized()
    for secret in (BUYER_EMAIL, BUYER_NAME, BUYER_ADDRESS, "3412987711"):
        assert secret not in sent
