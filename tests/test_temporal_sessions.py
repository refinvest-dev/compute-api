from datetime import date

import pytest

from refinvest.engine.temporal import (
    TemporalResolutionError,
    resolve_holding_exit_signal_session,
    resolve_lagged_signal_session,
    resolve_next_execution_session,
)


class StaticCalendar:
    def __init__(self, sessions: dict[str, tuple[date, ...]]) -> None:
        self._sessions = sessions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return self._sessions.get(asset, ())


@pytest.fixture
def calendar() -> StaticCalendar:
    return StaticCalendar(
        {
            "BTCUSDT": (
                date(2025, 1, 3),
                date(2025, 1, 4),
                date(2025, 1, 5),
                date(2025, 1, 6),
            ),
            "QQQ": (date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)),
        }
    )


def test_lag_uses_primary_signal_asset_sessions(calendar: StaticCalendar) -> None:
    assert resolve_lagged_signal_session(calendar, "BTCUSDT", date(2025, 1, 4), 2) == date(
        2025, 1, 6
    )


def test_holding_period_uses_primary_signal_asset_sessions(calendar: StaticCalendar) -> None:
    assert resolve_holding_exit_signal_session(
        calendar, "BTCUSDT", date(2025, 1, 3), 2
    ) == date(2025, 1, 5)


def test_btc_weekend_signal_uses_next_qqq_session(calendar: StaticCalendar) -> None:
    assert resolve_next_execution_session(calendar, "QQQ", date(2025, 1, 4)) == date(2025, 1, 6)


def test_execution_session_is_strictly_after_anchor(calendar: StaticCalendar) -> None:
    assert resolve_next_execution_session(calendar, "QQQ", date(2025, 1, 3)) == date(2025, 1, 6)


@pytest.mark.parametrize(
    ("function", "args"),
    [
        (resolve_lagged_signal_session, ("BTCUSDT", date(2025, 1, 2), 0)),
        (resolve_next_execution_session, ("QQQ", date(2025, 1, 7))),
    ],
)
def test_unresolvable_sessions_fail_fast(
    calendar: StaticCalendar, function: object, args: tuple[object, ...]
) -> None:
    with pytest.raises(TemporalResolutionError) as error:
        function(calendar, *args)  # type: ignore[operator]
    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"


def test_signal_session_offset_beyond_snapshot_fails_fast(calendar: StaticCalendar) -> None:
    with pytest.raises(TemporalResolutionError):
        resolve_holding_exit_signal_session(calendar, "BTCUSDT", date(2025, 1, 5), 2)


def test_unordered_calendar_sessions_are_rejected() -> None:
    calendar = StaticCalendar({"QQQ": (date(2025, 1, 6), date(2025, 1, 3))})
    with pytest.raises(TemporalResolutionError, match="ordered and unique"):
        resolve_next_execution_session(calendar, "QQQ", date(2025, 1, 2))
