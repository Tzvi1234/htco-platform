"""Operations reports (slice 7.9): the period a run covers, the report's key, and the rule that
every figure is the count of the rows it links to, or says its source is unavailable."""

import uuid
from datetime import UTC, datetime

import pytest

from htco_domain.reports import (
    ReportType,
    SectionFigure,
    SectionName,
    SourceRef,
    period_for,
    report_key,
)

ORG = uuid.UUID("00000000-0000-0000-0000-00000000c001")


def test_the_daily_report_covers_the_previous_whole_day() -> None:
    period = period_for(ReportType.DAILY, datetime(2026, 10, 27, 6, 0, tzinfo=UTC))

    assert period.start == datetime(2026, 10, 26, tzinfo=UTC)
    assert period.end == datetime(2026, 10, 27, tzinfo=UTC)
    assert period.label == "2026-10-26"


def test_the_daily_report_counts_the_day_in_the_organizations_time_zone() -> None:
    # 22:30 UTC on the 26th is already the 27th in Jerusalem: the report covers the 26th there.
    period = period_for(
        ReportType.DAILY, datetime(2026, 10, 26, 22, 30, tzinfo=UTC), timezone="Asia/Jerusalem"
    )

    assert period.label == "2026-10-26"
    assert period.start == datetime(2026, 10, 25, 22, 0, tzinfo=UTC)
    assert period.end == datetime(2026, 10, 26, 22, 0, tzinfo=UTC)


def test_the_weekly_report_covers_the_previous_iso_week() -> None:
    period = period_for(ReportType.WEEKLY, datetime(2026, 10, 28, 6, 0, tzinfo=UTC))  # Wednesday

    assert period.start == datetime(2026, 10, 19, tzinfo=UTC)  # Monday
    assert period.end == datetime(2026, 10, 26, tzinfo=UTC)
    assert period.label == "2026-W43"


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        period_for(ReportType.DAILY, datetime(2026, 10, 27, 6, 0))  # noqa: DTZ001 - the test


@pytest.mark.ac("AC-7.9.2")
def test_the_key_is_per_organization_type_and_period() -> None:
    now = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
    daily = period_for(ReportType.DAILY, now)

    assert report_key(ORG, daily) == report_key(ORG, period_for(ReportType.DAILY, now))
    assert report_key(ORG, daily) != report_key(ORG, period_for(ReportType.WEEKLY, now))
    assert report_key(ORG, daily) != report_key(uuid.uuid7(), daily)
    assert report_key(ORG, daily) == f"report:daily:{ORG}:2026-10-26"


@pytest.mark.ac("AC-7.9.1")
def test_a_figure_is_the_count_of_the_rows_it_links_to() -> None:
    refs = (SourceRef("etsy_orders", "a"), SourceRef("etsy_orders", "b"))
    figure = SectionFigure.of(SectionName.ORDERS_INGESTED, refs)

    assert figure.count == 2
    assert figure.refs == refs
    assert figure.available


@pytest.mark.ac("AC-7.9.1")
def test_an_unavailable_source_has_no_number_rather_than_zero() -> None:
    figure = SectionFigure.unavailable_source(SectionName.APPROVALS_PENDING, "not_built")

    assert figure.count is None
    assert not figure.available
    assert figure.unavailable == "not_built"
    assert SectionFigure.of(SectionName.APPROVALS_PENDING, ()).count == 0


@pytest.mark.ac("AC-7.9.1")
def test_a_figure_cannot_disagree_with_its_rows() -> None:
    with pytest.raises(ValueError, match="links"):
        SectionFigure(SectionName.ORDERS_INGESTED, 3, (SourceRef("etsy_orders", "a"),), None)
    with pytest.raises(ValueError, match="unavailable"):
        SectionFigure(SectionName.ORDERS_INGESTED, None, (), None)
    with pytest.raises(ValueError, match="unavailable"):
        SectionFigure(SectionName.ORDERS_INGESTED, 0, (), "not_built")


def test_a_reference_names_its_source_and_row() -> None:
    with pytest.raises(ValueError, match="source"):
        SourceRef("", "a")
    with pytest.raises(ValueError, match="source"):
        SourceRef("etsy_orders", "")
