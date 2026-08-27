from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.engine.metrics import evaluate_simple_metric
from refinvest.engine.validation.models import AssetSymbol, MetricReference, MetricType
from refinvest.marketdata.domain import (
    AssetSession,
    CompletedAssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)
from refinvest.marketdata.ports import MissingRequiredMarketDataError


class StaticCalendar:
    def __init__(self, completions: dict[str, tuple[CompletedAssetSession, ...]]) -> None:
        self._completions = completions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return tuple(item.asset_session.session_date for item in self._completions.get(asset, ()))

    def expected_session_completions(self, asset: str) -> tuple[CompletedAssetSession, ...]:
        return self._completions.get(asset, ())


class StaticMarketData:
    def __init__(self, lookups: dict[AssetSession, DailyBarLookup]) -> None:
        self._lookups = lookups

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        return self._lookups[AssetSession(asset, session_date)]


def completion(asset: str, session_date: date, completed_at: datetime) -> CompletedAssetSession:
    return CompletedAssetSession(AssetSession(asset, session_date), completed_at)


def bar(asset: str, session_date: date, close: str) -> DailyBar:
    return DailyBar(
        asset,
        session_date,
        Decimal("100"),
        Decimal("102"),
        Decimal("99"),
        Decimal(close),
        1000,
    )


def simple_metric(asset: AssetSymbol) -> MetricReference:
    return MetricReference(asset, MetricType.SIMPLE, None)


def test_simple_metric_uses_the_selected_anchor_close() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "501.25")
    calendar = StaticCalendar(
        {
            "QQQ": (
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar)}
    )
    assert evaluate_simple_metric(
        calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 3), simple_metric(AssetSymbol.QQQ)
    ) == Decimal("501.25")


def test_btc_weekend_signal_uses_qqq_last_completed_close() -> None:
    qqq_friday_bar = bar("QQQ", date(2025, 1, 3), "500")
    calendar = StaticCalendar(
        {
            "BTCUSDT": (
                completion("BTCUSDT", date(2025, 1, 4), datetime(2025, 1, 5, tzinfo=UTC)),
            ),
            "QQQ": (
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
                completion("QQQ", date(2025, 1, 6), datetime(2025, 1, 6, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {
            qqq_friday_bar.asset_session: DailyBarLookup(
                DailyBarAvailability.AVAILABLE, qqq_friday_bar
            )
        }
    )
    assert evaluate_simple_metric(
        calendar, market_data, AssetSymbol.BTCUSDT, date(2025, 1, 4), simple_metric(AssetSymbol.QQQ)
    ) == Decimal("500")


def test_expected_anchor_without_a_daily_bar_fails_fast() -> None:
    calendar = StaticCalendar(
        {
            "QQQ": (
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {
            AssetSession("QQQ", date(2025, 1, 3)): DailyBarLookup(
                DailyBarAvailability.MISSING_EXPECTED_SESSION
            )
        }
    )
    with pytest.raises(MissingRequiredMarketDataError) as error:
        evaluate_simple_metric(
            calendar,
            market_data,
            AssetSymbol.QQQ,
            date(2025, 1, 3),
            simple_metric(AssetSymbol.QQQ),
        )
    assert error.value.error_code == "MISSING_REQUIRED_DATA"


def test_simple_metric_is_deterministic_for_the_same_snapshot_inputs() -> None:
    vix_bar = bar("VIX", date(2025, 1, 3), "18.50")
    calendar = StaticCalendar(
        {
            "QQQ": (
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
            "VIX": (
                completion("VIX", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {vix_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, vix_bar)}
    )
    values = [
        evaluate_simple_metric(
            calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 3), simple_metric(AssetSymbol.VIX)
        )
        for _ in range(2)
    ]
    assert values == [Decimal("18.50"), Decimal("18.50")]
