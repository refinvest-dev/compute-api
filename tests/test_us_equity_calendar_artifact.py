from datetime import UTC, date, datetime

import pytest

from refinvest.ingestion.calendars.us_equity import (
    US_EQUITY_CALENDAR_SOURCE,
    UsEquityCalendarIngestionError,
    build_us_equity_calendar,
)
from refinvest.marketdata.domain import AssetSession, CalendarSessionState


class StaticSchedule:
    def __init__(self) -> None:
        self._sessions = (date(2025, 1, 2), date(2025, 1, 3))

    def sessions_in_range(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(item for item in self._sessions if start <= item <= end)

    def session_close(self, session: object) -> datetime:
        assert isinstance(session, date)
        return datetime(session.year, session.month, session.day, 21, tzinfo=UTC)


def test_builds_all_mvp_us_equity_sessions_with_pinned_provenance() -> None:
    artifact = build_us_equity_calendar(
        date(2025, 1, 1), date(2025, 1, 4), {}, schedule=StaticSchedule(), package_version="4.10.1"
    )

    assert artifact.provenance.source == US_EQUITY_CALENDAR_SOURCE
    assert artifact.provenance.version == "4.10.1"
    assert (
        artifact.sessions[AssetSession("QQQ", date(2025, 1, 2))].state
        is CalendarSessionState.EXPECTED
    )
    assert artifact.sessions[AssetSession("VIX", date(2025, 1, 2))].completed_at == datetime(
        2025, 1, 2, 21, tzinfo=UTC
    )
    assert (
        artifact.sessions[AssetSession("SPY", date(2025, 1, 4))].state
        is CalendarSessionState.NON_TRADING
    )


def test_official_closure_and_early_close_overrides_are_immutable_inputs() -> None:
    artifact = build_us_equity_calendar(
        date(2025, 1, 2),
        date(2025, 1, 3),
        {date(2025, 1, 2): None, date(2025, 1, 3): datetime(2025, 1, 3, 18, tzinfo=UTC)},
        schedule=StaticSchedule(),
        package_version="4.10.1",
    )

    assert (
        artifact.sessions[AssetSession("QQQ", date(2025, 1, 2))].state
        is CalendarSessionState.NON_TRADING
    )
    assert artifact.sessions[AssetSession("QQQ", date(2025, 1, 3))].completed_at == datetime(
        2025, 1, 3, 18, tzinfo=UTC
    )
    assert (
        artifact.provenance.content_hash
        == build_us_equity_calendar(
            date(2025, 1, 2),
            date(2025, 1, 3),
            {date(2025, 1, 2): None, date(2025, 1, 3): datetime(2025, 1, 3, 18, tzinfo=UTC)},
            schedule=StaticSchedule(),
            package_version="4.10.1",
        ).provenance.content_hash
    )


def test_rejects_ambiguous_override_outside_coverage_or_non_utc() -> None:
    with pytest.raises(UsEquityCalendarIngestionError, match="outside coverage"):
        build_us_equity_calendar(
            date(2025, 1, 2),
            date(2025, 1, 3),
            {date(2025, 1, 4): None},
            schedule=StaticSchedule(),
            package_version="4.10.1",
        )
    with pytest.raises(UsEquityCalendarIngestionError, match="exact UTC"):
        build_us_equity_calendar(
            date(2025, 1, 2),
            date(2025, 1, 3),
            {date(2025, 1, 3): datetime(2025, 1, 3, 18)},
            schedule=StaticSchedule(),
            package_version="4.10.1",
        )
