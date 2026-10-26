"""The worker deploys the Phase 6 handlers and runs the fulfillment schedule only when named."""

import pytest

from htco_application.fulfillment_schedule import (
    FULFILLMENT_SCHEDULE,
    FULFILLMENT_SHOP_JOB,
    INCIDENT_DETECTION,
    REVIEW_SCHEDULE,
    REVIEW_SHOP_JOB,
    TRACKING_SYNC,
)
from htco_platform.settings import load_settings
from htco_worker.main import fulfillment_handlers, fulfillment_schedule_handlers, worker_engine
from htco_worker.settings import SCHEDULES, WorkerSettings
from tests.job_fakes import MemoryJobQueue
from tests.worker.test_main import Directory, environment  # noqa: F401 - the fixture


class NoDatabase:
    """Handlers are built, never run, in these tests."""


def test_without_the_etsy_vault_only_incident_detection_is_deployed() -> None:
    handlers = fulfillment_handlers(NoDatabase(), None)  # type: ignore[arg-type]
    assert set(handlers) == {INCIDENT_DETECTION}


def test_with_the_vault_tracking_sync_is_deployed_too() -> None:
    handlers = fulfillment_handlers(NoDatabase(), object())  # type: ignore[arg-type]
    assert set(handlers) == {INCIDENT_DETECTION, TRACKING_SYNC}


def test_the_schedule_is_off_unless_named() -> None:
    engine = worker_engine(NoDatabase())  # type: ignore[arg-type]
    off = fulfillment_schedule_handlers(
        (), engine=engine, queue=MemoryJobQueue(), directory=Directory()
    )
    on = fulfillment_schedule_handlers(
        (FULFILLMENT_SCHEDULE.kind,), engine=engine, queue=MemoryJobQueue(), directory=Directory()
    )
    assert off == {}
    assert set(on) == {FULFILLMENT_SCHEDULE.kind, FULFILLMENT_SHOP_JOB}


def test_settings_accept_the_fulfillment_schedule(environment: pytest.MonkeyPatch) -> None:  # noqa: F811
    environment.setenv("HTCO_WORKER_SCHEDULES", FULFILLMENT_SCHEDULE.kind)
    settings = load_settings(WorkerSettings)
    assert settings.schedules == (FULFILLMENT_SCHEDULE.kind,)
    assert SCHEDULES[FULFILLMENT_SCHEDULE.kind] == FULFILLMENT_SCHEDULE


def test_the_review_schedule_is_off_unless_named_on_its_own() -> None:
    """Slice 6.9 (AC-6.9.2): the hourly low-review read has its own flag."""
    engine = worker_engine(NoDatabase())  # type: ignore[arg-type]
    inspect_only = fulfillment_schedule_handlers(
        (FULFILLMENT_SCHEDULE.kind,), engine=engine, queue=MemoryJobQueue(), directory=Directory()
    )
    reviews = fulfillment_schedule_handlers(
        (REVIEW_SCHEDULE.kind,), engine=engine, queue=MemoryJobQueue(), directory=Directory()
    )
    assert REVIEW_SHOP_JOB not in inspect_only
    assert set(reviews) == {REVIEW_SCHEDULE.kind, REVIEW_SHOP_JOB}
    assert SCHEDULES[REVIEW_SCHEDULE.kind] == REVIEW_SCHEDULE
