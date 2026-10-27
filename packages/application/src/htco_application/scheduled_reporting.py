"""Scheduled Reporting & Notifications (slice 7.9, FR-NTF-03): the `scheduled-reporting` handler.
Deterministic: no model, no Etsy call.

One run builds the operations report of one organization for the last closed day or ISO week
(`report_type` in the payload; the organization's `timezone`, UTC by default) before `as_of`, the
time the scheduler made the execution due: every attempt of one execution covers the same
period, however late a retry runs (Codex and quill2 on #196). Each section has
one source that returns the recorded rows of the period (activity records from slice 7.7, or the
tables they describe); the figure is the number of those rows and keeps their references
(AC-7.9.1). A source that cannot be read, or a section with no source yet, is shown as
unavailable, never as zero.

The report is stored once per organization, type and period (AC-7.9.2): a second run for the same
period finds the stored report and delivers that one again, and the delivery (the notification
path of slice 6.4, keyed by the report) sends it once. A crash between the store and the delivery
is recovered by the retry, which delivers the stored report.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.kernel import JobError, Offload, run_inline
from htco_domain.reports import (
    UNAVAILABLE_PATTERN,
    ReportPeriod,
    ReportType,
    SectionFigure,
    SectionName,
    SourceRef,
    period_for,
    report_key,
)

SCHEDULED_REPORTING = "scheduled-reporting"
NO_SOURCE = "no_source"


class SourceUnavailable(Exception):  # noqa: N818 - named for what happened
    """The section's source cannot be read now; the reason is a short code, never data."""


class ReportSource(Protocol):
    """One section's rows for an organization and period. Synchronous; run through `offload`."""

    @property
    def section(self) -> SectionName: ...

    def collect(self, organization_id: uuid.UUID, period: ReportPeriod) -> tuple[SourceRef, ...]:
        """Raises `SourceUnavailable` when the source cannot be read."""
        ...


@dataclass(frozen=True)
class OperationsReport:
    """Identifiers, counts and references only: no buyer data, no free text."""

    id: uuid.UUID
    organization_id: uuid.UUID
    key: str
    period: ReportPeriod
    figures: tuple[SectionFigure, ...]
    generated_at: datetime

    def figure(self, section: SectionName) -> SectionFigure:
        return next(f for f in self.figures if f.section is section)


class ReportStore(Protocol):
    def save_report(self, report: OperationsReport) -> tuple[OperationsReport, bool]:
        """Insert unless the key exists; returns the stored report and whether it is new."""
        ...


class ReportDelivery(Protocol):
    async def deliver(self, report: OperationsReport) -> None:
        """Idempotent by `report.key`: a second delivery of a report sends nothing."""
        ...


@dataclass
class ScheduledReporting:
    """The `scheduled-reporting` handler."""

    sources: tuple[ReportSource, ...]
    store: ReportStore
    delivery: ReportDelivery
    clock: Callable[[], datetime]
    offload: Offload = run_inline
    new_id: Callable[[], uuid.UUID] = uuid.uuid7

    def __post_init__(self) -> None:
        sections = [s.section for s in self.sources]
        if len(sections) != len(set(sections)):
            raise ValueError("each section has one source")

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        # The execution's own time, not this attempt's clock (Codex and quill2 on #196): a retry
        # after midnight, or after the Monday of a weekly report, keeps its period and its key.
        period = _period(ctx)
        by_section = {s.section: s for s in self.sources}
        figures = []
        for section in SectionName:
            source = by_section.get(section)
            figures.append(
                SectionFigure.unavailable_source(section, NO_SOURCE)
                if source is None
                else await self._figure(source, ctx.organization_id, period)
            )
        report = OperationsReport(
            id=self.new_id(),
            organization_id=ctx.organization_id,
            key=report_key(ctx.organization_id, period),
            period=period,
            figures=tuple(figures),
            generated_at=self.clock(),
        )
        stored, created = await self.offload(lambda: self.store.save_report(report))
        await self.delivery.deliver(stored)
        return Completed(
            {
                "report_id": str(stored.id),
                "key": stored.key,
                "created": created,
                "unavailable": sum(not f.available for f in stored.figures),
            }
        )

    async def _figure(
        self, source: ReportSource, organization_id: uuid.UUID, period: ReportPeriod
    ) -> SectionFigure:
        try:
            refs = await self.offload(lambda: source.collect(organization_id, period))
        except SourceUnavailable as error:
            # Only a code passes (sage2 on #196): any other message, which could hold SQL or
            # data, becomes "unavailable", so a bad message degrades instead of failing the run.
            reason = str(error)
            code = reason if UNAVAILABLE_PATTERN.fullmatch(reason) else "unavailable"
            return SectionFigure.unavailable_source(source.section, code)
        return SectionFigure.of(source.section, refs)


def _as_of(ctx: HandlerContext) -> datetime:
    """When the scheduler made the execution due: an ISO-8601 time with its offset."""
    raw = ctx.payload.get("as_of")
    if not isinstance(raw, str):
        raise JobError("a report names its as_of: when the scheduler made it due")
    try:
        as_of = datetime.fromisoformat(raw)
    except ValueError:
        raise JobError("a report's as_of is an ISO-8601 time") from None
    if as_of.tzinfo is None:
        raise JobError("a report's as_of names its offset")
    return as_of


def _period(ctx: HandlerContext) -> ReportPeriod:
    raw_type = ctx.payload.get("report_type")
    try:
        report_type = ReportType(str(raw_type))
    except ValueError:
        raise JobError("a report names its type: daily or weekly") from None
    timezone = ctx.payload.get("timezone", "UTC")
    if not isinstance(timezone, str):
        raise JobError("a report's time zone is a name")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        raise JobError("a report's time zone is unknown") from None
    return period_for(report_type, _as_of(ctx), timezone=timezone)
