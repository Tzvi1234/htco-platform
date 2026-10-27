"""Scheduled Reporting & Notifications (slice 7.9): the `scheduled-reporting` handler with
in-memory sources, store and delivery. Replays, an unavailable source, a store that fails and a
delivery lost after the report was stored. All values are invented."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.execution.gateway import UnavailableGateway
from htco_application.kernel import JobError
from htco_application.scheduled_reporting import (
    SCHEDULED_REPORTING,
    OperationsReport,
    ScheduledReporting,
    SourceUnavailable,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.reports import (
    ReportPeriod,
    ReportType,
    SectionName,
    SourceRef,
)

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000c001")
OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-00000000c002")
NOW = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)


@dataclass
class Rows:
    """A source over recorded rows: what it returns per organization and period."""

    section: SectionName
    refs: tuple[SourceRef, ...] = ()
    fail: bool = False
    calls: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    def collect(self, organization_id: uuid.UUID, period: ReportPeriod) -> tuple[SourceRef, ...]:
        self.calls.append((organization_id, period.label))
        if self.fail:
            raise SourceUnavailable("activity_store_unreachable")
        return self.refs


@dataclass
class MemoryReports:
    """One report per key, like the unique index; the first one stays."""

    reports: dict[str, OperationsReport] = field(default_factory=dict)
    fail: bool = False

    def save_report(self, report: OperationsReport) -> tuple[OperationsReport, bool]:
        if self.fail:
            raise ConnectionError("the database went away")
        existing = self.reports.get(report.key)
        if existing is not None:
            return existing, False
        self.reports[report.key] = report
        return report, True


@dataclass
class Deliveries:
    """The notification path: one notification and email per key (as 6.4's store keeps it)."""

    sent: dict[str, OperationsReport] = field(default_factory=dict)
    attempts: int = 0
    fail_once: bool = False

    async def deliver(self, report: OperationsReport) -> None:
        self.attempts += 1
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("the outbox write failed")
        self.sent.setdefault(report.key, report)


def orders(*ids: str) -> Rows:
    return Rows(SectionName.ORDERS_INGESTED, tuple(SourceRef("etsy_orders", i) for i in ids))


@dataclass
class Reporting:
    sources: list[Rows] = field(default_factory=lambda: [orders("o-1", "o-2")])
    store: MemoryReports = field(default_factory=MemoryReports)
    delivery: Deliveries = field(default_factory=Deliveries)
    now: datetime = NOW

    @property
    def handler(self) -> ScheduledReporting:
        return ScheduledReporting(
            sources=tuple(self.sources),
            store=self.store,
            delivery=self.delivery,
            clock=lambda: self.now,
        )

    def context(
        self, payload: dict[str, Any], *, organization_id: uuid.UUID = ORG
    ) -> HandlerContext:
        return HandlerContext(
            execution_id=uuid.uuid7(),
            automation_id=SCHEDULED_REPORTING,
            actor=ActorRef(ActorKind.SYSTEM, "scheduler"),
            organization_id=organization_id,
            shop_id=None,
            correlation_id="sched:report",
            causation_id=None,
            attempt=1,
            payload=payload,
            gateway=UnavailableGateway(),
        )


@pytest.mark.ac("AC-7.9.1")
async def test_ac_7_9_1_every_number_links_to_its_rows() -> None:
    reporting = Reporting()

    outcome = await reporting.handler(reporting.context({"report_type": "daily"}))

    [report] = reporting.store.reports.values()
    orders_figure = report.figure(SectionName.ORDERS_INGESTED)
    assert orders_figure.count == 2
    assert [r.id for r in orders_figure.refs] == ["o-1", "o-2"]
    assert outcome == Completed(
        {"report_id": str(report.id), "key": report.key, "created": True, "unavailable": 7}
    )


@pytest.mark.ac("AC-7.9.1")
async def test_ac_7_9_1_a_section_without_a_source_says_so_instead_of_zero() -> None:
    reporting = Reporting(sources=[orders(), Rows(SectionName.INCIDENTS_OPENED, fail=True)])

    await reporting.handler(reporting.context({"report_type": "daily"}))

    [report] = reporting.store.reports.values()
    assert report.figure(SectionName.ORDERS_INGESTED).count == 0  # read, and truly none
    failed = report.figure(SectionName.INCIDENTS_OPENED)
    assert failed.count is None
    assert failed.unavailable == "activity_store_unreachable"
    not_built = report.figure(SectionName.APPROVALS_PENDING)
    assert not_built.count is None
    assert not_built.unavailable == "no_source"
    assert [f.section for f in report.figures] == list(SectionName)


@pytest.mark.ac("AC-7.9.2")
async def test_ac_7_9_2_the_daily_report_twice_for_the_same_day_sends_one_report() -> None:
    reporting = Reporting()

    first = await reporting.handler(reporting.context({"report_type": "daily"}))
    reporting.sources[0].refs = (*reporting.sources[0].refs, SourceRef("etsy_orders", "o-3"))
    reporting.now = NOW.replace(hour=23)  # later the same day: the same period
    second = await reporting.handler(reporting.context({"report_type": "daily"}))

    [report] = reporting.store.reports.values()
    [sent] = reporting.delivery.sent.values()
    assert sent == report
    assert report.figure(SectionName.ORDERS_INGESTED).count == 2  # the first report stands
    assert isinstance(first, Completed)
    assert isinstance(second, Completed)
    assert first.output["created"] is True
    assert second.output["created"] is False
    assert second.output["key"] == first.output["key"]


@pytest.mark.ac("AC-7.9.2")
async def test_ac_7_9_2_other_days_types_and_organizations_are_other_reports() -> None:
    reporting = Reporting()

    await reporting.handler(reporting.context({"report_type": "daily"}))
    await reporting.handler(reporting.context({"report_type": "weekly"}))
    await reporting.handler(reporting.context({"report_type": "daily"}, organization_id=OTHER_ORG))
    reporting.now = NOW.replace(day=28)
    await reporting.handler(reporting.context({"report_type": "daily"}))

    assert sorted(reporting.store.reports) == sorted(
        [
            f"report:daily:{ORG}:2026-10-26",
            f"report:weekly:{ORG}:2026-W43",
            f"report:daily:{OTHER_ORG}:2026-10-26",
            f"report:daily:{ORG}:2026-10-27",
        ]
    )
    assert len(reporting.delivery.sent) == 4
    # Each source is asked for its own organization only.
    assert (OTHER_ORG, "2026-10-26") in reporting.sources[0].calls


async def test_a_delivery_lost_after_the_report_was_stored_is_delivered_on_retry() -> None:
    reporting = Reporting(delivery=Deliveries(fail_once=True))

    with pytest.raises(ConnectionError):
        await reporting.handler(reporting.context({"report_type": "daily"}))
    await reporting.handler(reporting.context({"report_type": "daily"}))

    [report] = reporting.store.reports.values()
    assert reporting.delivery.sent == {report.key: report}
    assert reporting.delivery.attempts == 2


async def test_a_store_failure_sends_nothing_and_fails_the_attempt() -> None:
    reporting = Reporting(store=MemoryReports(fail=True))

    with pytest.raises(ConnectionError):
        await reporting.handler(reporting.context({"report_type": "daily"}))

    assert reporting.delivery.attempts == 0


async def test_the_organizations_time_zone_picks_its_day() -> None:
    reporting = Reporting(now=datetime(2026, 10, 26, 22, 30, tzinfo=UTC))

    await reporting.handler(
        reporting.context({"report_type": "daily", "timezone": "Asia/Jerusalem"})
    )

    [key] = reporting.store.reports
    assert key.endswith(":2026-10-26")


@pytest.mark.parametrize(
    "payload",
    [{}, {"report_type": "monthly"}, {"report_type": "daily", "timezone": "Mars/Base"},
     {"report_type": "daily", "timezone": 3}],
)  # fmt: skip
async def test_bad_payloads_are_refused(payload: dict[str, Any]) -> None:
    reporting = Reporting()

    with pytest.raises(JobError):
        await reporting.handler(reporting.context(payload))

    assert reporting.store.reports == {}


def test_a_second_source_for_one_section_is_refused() -> None:
    with pytest.raises(ValueError, match="one source"):
        ScheduledReporting(
            sources=(orders(), orders()),
            store=MemoryReports(),
            delivery=Deliveries(),
            clock=lambda: NOW,
        )


def test_the_report_carries_identifiers_only() -> None:
    assert ReportType.DAILY.value == "daily"
    fields = set(OperationsReport.__dataclass_fields__)
    assert fields == {"id", "organization_id", "key", "period", "figures", "generated_at"}
