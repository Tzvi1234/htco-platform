"""The `JobQueue` port's values (slice 5.1): job specs, stored errors, schedule jobs."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.jobs import JobError, JobSpec, describe_error, due_schedule_jobs
from htco_domain.jobs import Schedule
from tests.job_fakes import MemoryJobQueue

NOW = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
ORG = uuid.UUID("00000000-0000-7000-8000-00000000000a")
SHOP = uuid.UUID("00000000-0000-7000-8000-00000000000b")


def test_job_spec__shop_without_its_organization__is_rejected() -> None:
    with pytest.raises(ValueError, match="organization"):
        JobSpec(kind="etsy.capability_probe", idempotency_key="k", shop_id=SHOP)


@pytest.mark.parametrize("key", ["", "   ", "k" * 201])
def test_job_spec__blank_or_long_key__is_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="job key"):
        JobSpec(kind="etsy.capability_probe", idempotency_key=key)


@pytest.mark.parametrize("lock", ["", "l" * 201])
def test_job_spec__empty_or_long_lock_key__is_rejected(lock: str) -> None:
    with pytest.raises(ValueError, match="lock key"):
        JobSpec(kind="etsy.capability_probe", idempotency_key="k", lock_key=lock)


def test_job_spec__bad_kind__is_rejected() -> None:
    with pytest.raises(ValueError, match="job kind"):
        JobSpec(kind="Probe", idempotency_key="k")


def test_job_spec__a_shops_job__is_accepted() -> None:
    spec = JobSpec(
        kind="etsy.capability_probe",
        idempotency_key="k",
        organization_id=ORG,
        shop_id=SHOP,
        lock_key=f"shop:{SHOP}:etsy",
    )

    assert spec.payload == {}
    assert spec.retry.max_attempts == 3


@pytest.mark.ac("AC-5.1.3")
def test_describe_error__an_unmarked_exception__keeps_only_its_type() -> None:
    error = RuntimeError("buyer FAKE-BUYER-NAME-ZZZ at FAKE-LINE-ONE-ZZZ")

    assert describe_error(error) == "RuntimeError"


@pytest.mark.ac("AC-5.1.3")
def test_describe_error__a_job_error__keeps_its_safe_message_cut_to_500() -> None:
    assert describe_error(JobError("shop not connected")) == "JobError: shop not connected"
    assert len(describe_error(JobError("x" * 2000))) == 500


@pytest.mark.ac("AC-5.1.6")
def test_due_schedule_jobs__enqueued_twice_in_one_period__make_one_job() -> None:
    queue = MemoryJobQueue()
    daily = Schedule("maintenance.capability_probes", timedelta(days=1))

    for tick in (NOW, NOW + timedelta(hours=3)):
        for spec in due_schedule_jobs([daily], tick):
            queue.enqueue(spec, now=tick)

    job = queue.only()
    assert job.spec.idempotency_key == "maintenance.capability_probes@2026-10-22T00:00Z"
    assert job.run_at == datetime(2026, 10, 22, tzinfo=UTC)


@pytest.mark.parametrize("field", ["correlation_id", "causation_id"])
@pytest.mark.parametrize("value", ["", "c" * 201])
def test_job_spec__blank_or_long_correlation_or_causation__is_rejected(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError, match=field):
        JobSpec(kind="etsy.capability_probe", idempotency_key="k", **{field: value})  # type: ignore[arg-type]


def test_job_spec__carries_correlation_and_causation() -> None:
    spec = JobSpec(
        kind="automation.execute",
        idempotency_key="k",
        correlation_id="req-1",
        causation_id="execution:42",
    )

    assert (spec.correlation_id, spec.causation_id) == ("req-1", "execution:42")
