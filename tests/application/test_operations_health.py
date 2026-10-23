"""Queue, execution and sync health for the Owner and Admins (slice 5.9), on in-memory ports."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from htco_application.audit import AuditRecord
from htco_application.operations_health import (
    EXECUTION_JOB_KIND,
    DeadJob,
    DeadJobNotFoundError,
    ExecutionStat,
    FoundJob,
    HealthSnapshot,
    JobCount,
    JobNotDeadError,
    OperationsForbiddenError,
    RetryThroughExecutionError,
    ShopSync,
    WorkerBeat,
    automation_metrics,
    operations_health,
    retry_dead_job,
)
from htco_domain.identity import Role
from tests.application.test_shop_dashboard import ORG, OTHER_ORG, SHOP, auth

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
JOB = uuid.UUID("01a00000-0000-7000-8000-0000000000a1")
EXECUTION = uuid.UUID("01a00000-0000-7000-8000-0000000000e1")


def snapshot(**changes: object) -> HealthSnapshot:
    values: dict[str, object] = {
        "jobs": (
            JobCount("etsy.webhook_event", "dead", 1),
            JobCount("automation.execute", "queued", 2),
        ),
        "dead_letters": (
            DeadJob(
                JOB,
                "etsy.webhook_event",
                SHOP,
                8,
                "MarketplaceUnavailableError",
                NOW - timedelta(hours=1),
                None,
            ),
        ),
        "workers": (
            WorkerBeat(
                "agent-worker:a",
                "abc",
                NOW - timedelta(hours=3),
                NOW - timedelta(seconds=20),
                None,
                False,
            ),
            WorkerBeat(
                "agent-worker:b",
                "abc",
                NOW - timedelta(hours=9),
                NOW - timedelta(minutes=9),
                None,
                False,
            ),
        ),
        "shops": (
            ShopSync(
                SHOP,
                "orders",
                "sweep",
                NOW - timedelta(hours=2),
                NOW - timedelta(hours=2),
                None,
                1,
                3,
            ),
        ),
        "webhooks": (),
        "executions": (),
        "outbox_failures": (),
        "execution_stats": (),
    } | changes
    return HealthSnapshot(**values)  # type: ignore[arg-type]


@dataclass
class Reader:
    found: HealthSnapshot = field(default_factory=snapshot)
    asked: list[uuid.UUID] = field(default_factory=list)

    def snapshot(self, organization_id: uuid.UUID, *, now: datetime) -> HealthSnapshot:  # noqa: ARG002
        self.asked.append(organization_id)
        return self.found


@dataclass
class Jobs:
    jobs: dict[tuple[uuid.UUID, uuid.UUID], FoundJob] = field(default_factory=dict)
    audits: list[AuditRecord] = field(default_factory=list)

    def find(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> FoundJob | None:
        return self.jobs.get((organization_id, job_id))

    def retry(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        now: datetime,  # noqa: ARG002 - the port
        audit: AuditRecord,
    ) -> bool:
        job = self.jobs[(organization_id, job_id)]
        if job.state != "dead":
            return False
        self.jobs[(organization_id, job_id)] = replace(job, state="queued", attempts=0)
        self.audits.append(audit)
        return True


def dead(kind: str = "etsy.webhook_event", state: str = "dead") -> Jobs:
    return Jobs(
        {
            (ORG, JOB): FoundJob(
                JOB,
                kind,
                state,
                8,
                "MarketplaceUnavailableError",
                EXECUTION if kind == EXECUTION_JOB_KIND else None,
            )
        }
    )


# --- who sees it ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
def test_health__owner_and_admin__see_their_organization(role: Role) -> None:
    reader = Reader()

    found = operations_health(reader, auth(role), now=NOW)

    assert found.organization_id == ORG
    assert reader.asked == [ORG]


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.FINANCE, Role.VIEWER])
def test_health__other_roles__forbidden_and_nothing_read(role: Role) -> None:
    reader = Reader()

    with pytest.raises(OperationsForbiddenError):
        operations_health(reader, auth(role), now=NOW)
    assert reader.asked == []


# --- what it shows --------------------------------------------------------------------------


@pytest.mark.ac("AC-5.9.1")
def test_health__a_worker_past_the_threshold__shown_as_stopped() -> None:
    found = operations_health(Reader(), auth(), now=NOW)

    assert [(w.beat.worker_id, w.alive, w.age_seconds) for w in found.workers] == [
        ("agent-worker:a", True, 20),
        ("agent-worker:b", False, 540),
    ]


@pytest.mark.ac("AC-5.9.3")
def test_health__a_shop_past_its_sync_window__shown_as_stale() -> None:
    old = ShopSync(
        SHOP,
        "orders",
        "reconcile",
        NOW - timedelta(hours=14),
        NOW - timedelta(hours=14),
        "ShopBusyError",
        0,
        0,
    )
    never = replace(old, shop_id=uuid.uuid4(), last_succeeded_at=None)
    fresh = replace(old, shop_id=uuid.uuid4(), last_succeeded_at=NOW - timedelta(hours=1))

    found = operations_health(Reader(snapshot(shops=(old, never, fresh))), auth(), now=NOW)

    assert [s.stale for s in found.shops] == [True, True, False]


@pytest.mark.ac("AC-5.9.2")
def test_health__dead_letters_with_their_last_error() -> None:
    found = operations_health(Reader(), auth(), now=NOW)

    [letter] = found.dead_letters
    assert (letter.id, letter.kind, letter.last_error) == (
        JOB,
        "etsy.webhook_event",
        "MarketplaceUnavailableError",
    )


def test_metrics__per_automation_success_rate_retries_dead_letters_and_latency() -> None:
    stats = [
        *(ExecutionStat("etsy-order-intake", "succeeded", 1, float(s)) for s in range(1, 20)),
        ExecutionStat("etsy-order-intake", "succeeded", 3, 100.0),
        ExecutionStat("etsy-order-intake", "dead_lettered", 5, 300.0),
        ExecutionStat("etsy-order-intake", "running", 1, None),
        ExecutionStat("product-sales-counters", "succeeded", 1, 2.0),
    ]

    intake, counters = automation_metrics(stats)

    assert (intake.automation_id, intake.executions, intake.succeeded, intake.dead_lettered) == (
        "etsy-order-intake",
        22,
        20,
        1,
    )
    assert intake.retries == 2 + 4
    assert intake.success_rate == Decimal("95.2")
    assert intake.p50_seconds == 11.0
    assert intake.p95_seconds is not None
    assert 100.0 <= intake.p95_seconds <= 300.0
    assert (counters.p50_seconds, counters.p95_seconds, counters.success_rate) == (
        2.0,
        2.0,
        Decimal("100.0"),
    )


def test_metrics__nothing_finished__no_rate_and_no_latency() -> None:
    [only] = automation_metrics([ExecutionStat("x", "running", 1, None)])

    assert (only.success_rate, only.p50_seconds, only.p95_seconds) == (None, None, None)


# --- the audited retry ----------------------------------------------------------------------


@pytest.mark.ac("AC-5.9.2")
def test_retry__a_dead_job__queued_again_and_audited_without_its_payload() -> None:
    jobs = dead()

    retry_dead_job(jobs, auth(Role.ADMIN), JOB, correlation_id="req-7", now=NOW)

    assert jobs.jobs[(ORG, JOB)].state == "queued"
    [audit] = jobs.audits
    assert (audit.action, audit.entity_type, audit.entity_id, audit.correlation_id) == (
        "job.dead_letter_retried",
        "job",
        str(JOB),
        "req-7",
    )
    assert audit.before == {
        "state": "dead",
        "attempts": 8,
        "last_error": "MarketplaceUnavailableError",
    }
    assert "payload" not in str(audit.after)


def test_retry__an_execution_job__through_its_execution() -> None:
    with pytest.raises(RetryThroughExecutionError) as refused:
        retry_dead_job(dead(EXECUTION_JOB_KIND), auth(), JOB, correlation_id="c", now=NOW)

    assert refused.value.execution_id == EXECUTION


def test_retry__a_job_that_is_not_dead__refused_and_not_audited() -> None:
    jobs = dead(state="retry_scheduled")

    with pytest.raises(JobNotDeadError):
        retry_dead_job(jobs, auth(), JOB, correlation_id="c", now=NOW)
    assert jobs.audits == []


def test_retry__another_organizations_job__not_found() -> None:
    with pytest.raises(DeadJobNotFoundError):
        retry_dead_job(dead(), auth(org=OTHER_ORG), JOB, correlation_id="c", now=NOW)


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.VIEWER])
def test_retry__other_roles__forbidden(role: Role) -> None:
    jobs = dead()

    with pytest.raises(OperationsForbiddenError):
        retry_dead_job(jobs, auth(role), JOB, correlation_id="c", now=NOW)
    assert jobs.jobs[(ORG, JOB)].state == "dead"
