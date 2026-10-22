"""Technical jobs (ADR-0007): at-least-once units of work that a worker claims with a lease.

A job is "ingest receipt 123", "deliver outbox message 456" or "probe shop 789 today". It is not
a business task: its states describe the queue, not the business. A worker claims a queued job
for a lease and extends the lease while it runs; a job whose worker vanished is released when
the lease expires and runs again, so every job handler must be idempotent (ER-REL-01). A job
that keeps failing ends `dead` (the dead-letter state) with its last error, until an admin puts
it back in the queue.

Scheduled maintenance is a job per period: the scheduler names each period's job with a
deterministic key, so two workers enqueueing the same period create one job (AC-5.1.6).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from htco_domain.automation import RetryPolicy
from htco_domain.state_machine import check_transition


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    DEAD = "dead"  # the dead-letter state: retries are exhausted


_JOB_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED, JobState.RETRY_SCHEDULED, JobState.DEAD}),
    JobState.RETRY_SCHEDULED: frozenset({JobState.RUNNING}),
    JobState.DEAD: frozenset({JobState.QUEUED}),  # an admin puts it back (audited)
}


def check_job_transition(current: JobState, target: JobState) -> None:
    check_transition("job", _JOB_TRANSITIONS, current, target)


# `<area>.<action>`, for example `maintenance.capability_probes` or `etsy.capability_probe`.
_JOB_KIND = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
MAX_KIND_LENGTH = 64
MAX_KEY_LENGTH = 200


def check_job_kind(kind: str) -> None:
    if len(kind) > MAX_KIND_LENGTH or not _JOB_KIND.match(kind):
        raise ValueError(f"invalid job kind {kind!r} (expected <area>.<action>, lower case)")


@dataclass(frozen=True)
class FailurePlan:
    """What happens to a job after a failed attempt."""

    state: JobState  # RETRY_SCHEDULED or DEAD
    retry_at: datetime | None


def plan_after_failure(
    *, attempts: int, policy: RetryPolicy, now: datetime, jitter: float
) -> FailurePlan:
    """After `attempts` failed attempts: dead when the policy's attempts are used up, otherwise
    retried after the policy's backoff with jitter (`jitter` is a random number in [0, 1])."""
    if attempts >= policy.max_attempts:
        return FailurePlan(JobState.DEAD, None)
    return FailurePlan(JobState.RETRY_SCHEDULED, now + policy.jittered_backoff(attempts, jitter))


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Schedule:
    """A recurring job: one job of `kind` per period of length `every`, aligned to UTC."""

    kind: str
    every: timedelta

    def __post_init__(self) -> None:
        check_job_kind(self.kind)
        if self.every < timedelta(minutes=1):
            raise ValueError("a schedule runs at most once a minute")

    def period_start(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("schedules need a timezone-aware clock")
        elapsed = now.astimezone(UTC) - _EPOCH
        return _EPOCH + (elapsed // self.every) * self.every

    def key_for(self, now: datetime) -> str:
        """The period's job key: equal for every worker and every tick of the same period."""
        return f"{self.kind}@{self.period_start(now).strftime('%Y-%m-%dT%H:%MZ')}"
