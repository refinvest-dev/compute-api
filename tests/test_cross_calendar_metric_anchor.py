from datetime import UTC, date, datetime

import pytest

from refinvest.engine.temporal import TemporalResolutionError, resolve_metric_anchor_session
from refinvest.marketdata.domain import AssetSession, CompletedAssetSession


class StaticCompletionCalendar:
    def __init__(self, completions: dict[str, tuple[CompletedAssetSession, ...]]) -> None:
        self._completions = completions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return tuple(item.asset_session.session_date for item in self._completions.get(asset, ()))

    def expected_session_completions(self, asset: str) -> tuple[CompletedAssetSession, ...]:
        return self._completions.get(asset, ())


def completion(asset: str, session_date: date, completed_at: datetime) -> CompletedAssetSession:
    return CompletedAssetSession(AssetSession(asset, session_date), completed_at)


@pytest.fixture
def calendar() -> StaticCompletionCalendar:
    return StaticCompletionCalendar(
        {
            "BTCUSDT": (
                completion("BTCUSDT", date(2025, 1, 3), datetime(2025, 1, 4, tzinfo=UTC)),
                completion("BTCUSDT", date(2025, 1, 4), datetime(2025, 1, 5, tzinfo=UTC)),
            ),
            "QQQ": (
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
                completion("QQQ", date(2025, 1, 6), datetime(2025, 1, 6, 21, tzinfo=UTC)),
            ),
            "SPY": (completion("SPY", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )


def test_btc_weekend_signal_anchors_qqq_to_the_last_completed_session(
    calendar: StaticCompletionCalendar,
) -> None:
    assert resolve_metric_anchor_session(calendar, "BTCUSDT", date(2025, 1, 4), "QQQ") == date(
        2025, 1, 3
    )


def test_equal_completion_time_is_available_to_the_metric(
    calendar: StaticCompletionCalendar,
) -> None:
    assert resolve_metric_anchor_session(calendar, "QQQ", date(2025, 1, 3), "SPY") == date(
        2025, 1, 3
    )


def test_future_reference_session_is_not_selected(calendar: StaticCompletionCalendar) -> None:
    assert resolve_metric_anchor_session(calendar, "BTCUSDT", date(2025, 1, 3), "QQQ") == date(
        2025, 1, 3
    )


def test_missing_completed_reference_session_fails_fast() -> None:
    calendar = StaticCompletionCalendar(
        {
            "BTCUSDT": (completion("BTCUSDT", date(2025, 1, 3), datetime(2025, 1, 4, tzinfo=UTC)),),
            "QQQ": (completion("QQQ", date(2025, 1, 6), datetime(2025, 1, 6, 21, tzinfo=UTC)),),
        }
    )
    with pytest.raises(TemporalResolutionError) as error:
        resolve_metric_anchor_session(calendar, "BTCUSDT", date(2025, 1, 3), "QQQ")
    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"


def test_missing_primary_signal_completion_fails_fast(calendar: StaticCompletionCalendar) -> None:
    with pytest.raises(TemporalResolutionError) as error:
        resolve_metric_anchor_session(calendar, "BTCUSDT", date(2025, 1, 2), "QQQ")
    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"


def test_unordered_reference_completion_times_fail_fast() -> None:
    calendar = StaticCompletionCalendar(
        {
            "BTCUSDT": (completion("BTCUSDT", date(2025, 1, 3), datetime(2025, 1, 4, tzinfo=UTC)),),
            "QQQ": (
                completion("QQQ", date(2025, 1, 2), datetime(2025, 1, 3, tzinfo=UTC)),
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 2, tzinfo=UTC)),
            ),
        }
    )
    with pytest.raises(TemporalResolutionError, match="strictly ordered"):
        resolve_metric_anchor_session(calendar, "BTCUSDT", date(2025, 1, 3), "QQQ")
