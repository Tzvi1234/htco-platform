"""Operations reports (slice 7.9, FR-NTF-03). No I/O.

A report covers one closed period: the previous whole day, or the previous ISO week, in the
organization's time zone. It is keyed by organization, type and period, so running it twice for
the same period sends one report (AC-7.9.2). Each section is a figure computed from recorded rows
only: the figure is the number of rows it links to, never a separate number, and a section whose
source is unavailable has no number at all, never a zero (AC-7.9.1).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

MAX_REF_LENGTH = 120


class ReportType(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class SectionName(StrEnum):
    """The operations summary of slice 7.9, in the order it is shown."""

    ORDERS_INGESTED = "orders_ingested"
    INCIDENTS_OPENED = "incidents_opened"
    INCIDENTS_RESOLVED = "incidents_resolved"
    APPROVALS_PENDING = "approvals_pending"
    APPROVALS_EXPIRED = "approvals_expired"
    EXECUTIONS_FAILED = "executions_failed"
    EXECUTIONS_DEAD_LETTERED = "executions_dead_lettered"
    STALE_SHOPS = "stale_shops"


@dataclass(frozen=True)
class ReportPeriod:
    type: ReportType
    start: datetime  # inclusive, UTC
    end: datetime  # exclusive, UTC
    label: str  # "2026-10-26" or "2026-W43"


def period_for(report_type: ReportType, now: datetime, *, timezone: str = "UTC") -> ReportPeriod:
    """The last closed period before `now`, counted in `timezone`."""
    if now.tzinfo is None:
        raise ValueError("now needs a timezone")
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    if report_type is ReportType.DAILY:
        first = today - timedelta(days=1)
        last = today
        label = first.isoformat()
    else:
        last = today - timedelta(days=today.weekday())  # this week's Monday
        first = last - timedelta(days=7)
        year, week, _ = first.isocalendar()
        label = f"{year}-W{week:02d}"
    return ReportPeriod(
        type=report_type,
        start=datetime.combine(first, time(), zone).astimezone(ZoneInfo("UTC")),
        end=datetime.combine(last, time(), zone).astimezone(ZoneInfo("UTC")),
        label=label,
    )


def report_key(organization_id: uuid.UUID, period: ReportPeriod) -> str:
    return f"report:{period.type.value}:{organization_id}:{period.label}"


@dataclass(frozen=True)
class SourceRef:
    """One row a figure was computed from: its source (an activity kind or a table) and ID."""

    source: str
    id: str

    def __post_init__(self) -> None:
        if not self.source or not self.id or max(len(self.source), len(self.id)) > MAX_REF_LENGTH:
            raise ValueError(f"a reference names its source and row (1..{MAX_REF_LENGTH})")


@dataclass(frozen=True)
class SectionFigure:
    section: SectionName
    count: int | None
    refs: tuple[SourceRef, ...]
    unavailable: str | None  # why the source could not be read; None when it was

    def __post_init__(self) -> None:
        if self.unavailable is None:
            if self.count is None:
                raise ValueError("a figure without a number says why its source is unavailable")
            if self.count != len(self.refs):
                raise ValueError("a figure is the number of rows it links to")
        elif self.count is not None or self.refs:
            raise ValueError("an unavailable source has no number and no rows")

    @property
    def available(self) -> bool:
        return self.unavailable is None

    @classmethod
    def of(cls, section: SectionName, refs: tuple[SourceRef, ...]) -> SectionFigure:
        return cls(section, len(refs), refs, None)

    @classmethod
    def unavailable_source(cls, section: SectionName, reason: str) -> SectionFigure:
        return cls(section, None, (), reason or "unavailable")
