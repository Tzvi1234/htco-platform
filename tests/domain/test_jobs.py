"""Technical jobs (slice 5.1): the job state machine, retries with jitter, schedule periods."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_domain.automation import RetryPolicy
from htco_domain.jobs import (
    FailurePlan,
    JobState,
    Schedule,
    check_job_kind,
    check_job_transition,
    plan_after_failure,
)
from htco_domain.state_machine import IllegalTransition

NOW = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
POLICY = RetryPolicy(
    max_attempts=4, initial_backoff=timedelta(seconds=10), max_backoff=timedelta(minutes=5)
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.RETRY_SCHEDULED),
        (JobState.RUNNING, JobState.DEAD),
        (JobState.RETRY_SCHEDULED, JobState.RUNNING),
        (JobState.DEAD, JobState.QUEUED),
    ],
)
def test_job_transition__allowed_edge__passes(current: JobState, target: JobState) -> None:
    check_job_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobState.SUCCEEDED, JobState.RUNNING),
        (JobState.SUCCEEDED, JobState.QUEUED),
        (JobState.QUEUED, JobState.SUCCEEDED),
        (JobState.DEAD, JobState.RUNNING),
        (JobState.RETRY_SCHEDULED, JobState.SUCCEEDED),
    ],
)
def test_job_transition__any_other_edge__is_rejected(current: JobState, target: JobState) -> None:
    with pytest.raises(IllegalTransition):
        check_job_transition(current, target)


@pytest.mark.ac("AC-5.1.3")
def test_plan_after_failure__attempts_used_up__is_dead_without_a_retry_time() -> None:
    assert plan_after_failure(attempts=4, policy=POLICY, now=NOW, jitter=0.5) == FailurePlan(
        JobState.DEAD, None
    )


def test_plan_after_failure__attempts_left__retries_after_the_jittered_backoff() -> None:
    plan = plan_after_failure(attempts=2, policy=POLICY, now=NOW, jitter=1.0)

    assert plan == FailurePlan(JobState.RETRY_SCHEDULED, NOW + timedelta(seconds=20))


@given(
    attempt=st.integers(min_value=1, max_value=30),
    jitter=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_jittered_backoff__stays_between_half_and_all_of_the_capped_backoff(
    attempt: int, jitter: float
) -> None:
    full = POLICY.backoff(attempt)

    delay = POLICY.jittered_backoff(attempt, jitter)

    assert full / 2 <= delay <= full <= POLICY.max_backoff


@pytest.mark.parametrize("jitter", [-0.1, 1.1])
def test_jittered_backoff__jitter_outside_zero_to_one__is_rejected(jitter: float) -> None:
    with pytest.raises(ValueError, match="jitter"):
        POLICY.jittered_backoff(1, jitter)


@pytest.mark.parametrize(
    "kind", ["maintenance.capability_probes", "etsy.capability_probe", "outbox.deliver"]
)
def test_job_kind__area_and_action__is_accepted(kind: str) -> None:
    check_job_kind(kind)


@pytest.mark.parametrize("kind", ["probe", "Etsy.probe", "etsy..probe", "etsy.probe!", "a." * 40])
def test_job_kind__anything_else__is_rejected(kind: str) -> None:
    with pytest.raises(ValueError, match="job kind"):
        check_job_kind(kind)


@pytest.mark.ac("AC-5.1.6")
def test_schedule__every_tick_of_one_period__has_the_same_key() -> None:
    daily = Schedule("maintenance.capability_probes", timedelta(days=1))
    morning = datetime(2026, 10, 22, 0, 0, 1, tzinfo=UTC)
    evening = datetime(2026, 10, 22, 23, 59, 59, tzinfo=UTC)

    assert daily.key_for(morning) == daily.key_for(evening)
    assert daily.key_for(evening) == "maintenance.capability_probes@2026-10-22T00:00Z"
    assert daily.key_for(evening + timedelta(seconds=1)).endswith("2026-10-23T00:00Z")


@given(st.datetimes(timezones=st.just(UTC), min_value=datetime(2000, 1, 1)))  # noqa: DTZ001 - hypothesis bounds are naive
def test_schedule__period_start__is_at_or_before_now_and_within_one_period(now: datetime) -> None:
    hourly = Schedule("maintenance.refresh_token_expiry", timedelta(hours=1))

    start = hourly.period_start(now)

    assert start <= now < start + hourly.every


def test_schedule__naive_clock__is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Schedule("maintenance.purge", timedelta(days=1)).key_for(
            datetime(2026, 10, 22)  # noqa: DTZ001 - the naive clock under test
        )


def test_schedule__more_often_than_a_minute__is_rejected() -> None:
    with pytest.raises(ValueError, match="once a minute"):
        Schedule("maintenance.purge", timedelta(seconds=10))
