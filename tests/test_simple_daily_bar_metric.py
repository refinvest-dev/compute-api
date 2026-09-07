from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.engine.metrics import (
    evaluate_lookback_metric,
    evaluate_metric,
    evaluate_simple_metric,
)
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.validation.models import AssetSymbol, MetricReference, MetricType
from refinvest.marketdata.domain import (
    AssetSession,
    ClosePriceLookup,
    CompletedAssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)
from refinvest.marketdata.ports import DatasetCorruptionError, MissingRequiredMarketDataError


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

    def get_close(self, asset: str, session_date: date) -> ClosePriceLookup:
        lookup = self.get_daily_bar(asset, session_date)
        return ClosePriceLookup(
            lookup.availability, lookup.bar.close if lookup.bar is not None else None
        )


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


def lookback_metric(asset: AssetSymbol, metric_type: MetricType, window: int) -> MetricReference:
    return MetricReference(asset, metric_type, window)


def test_simple_metric_uses_the_selected_anchor_close() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "501.25")
    calendar = StaticCalendar(
        {
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
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
            "BTCUSDT": (completion("BTCUSDT", date(2025, 1, 4), datetime(2025, 1, 5, tzinfo=UTC)),),
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
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
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
    spy_bar = bar("SPY", date(2025, 1, 3), "18.50")
    calendar = StaticCalendar(
        {
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
            "SPY": (completion("SPY", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )
    market_data = StaticMarketData(
        {spy_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, spy_bar)}
    )
    values = [
        evaluate_simple_metric(
            calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 3), simple_metric(AssetSymbol.SPY)
        )
        for _ in range(2)
    ]
    assert values == [Decimal("18.50"), Decimal("18.50")]


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (MetricReference(AssetSymbol.QQQ, MetricType.SIMPLE, None), Decimal("121")),
        (MetricReference(AssetSymbol.QQQ, MetricType.RETURN, 2), Decimal("0.21")),
        (MetricReference(AssetSymbol.QQQ, MetricType.CHANGE, 2), Decimal("21")),
    ],
)
def test_metric_dispatches_each_supported_metric_type(
    metric: MetricReference, expected: Decimal
) -> None:
    qqq_bars = [
        bar("QQQ", date(2025, 1, 2), "100"),
        bar("QQQ", date(2025, 1, 3), "110"),
        bar("QQQ", date(2025, 1, 6), "121"),
    ]
    calendar = StaticCalendar(
        {
            "QQQ": tuple(
                completion(
                    "QQQ",
                    daily_bar.session_date,
                    datetime.combine(daily_bar.session_date, datetime.min.time(), UTC),
                )
                for daily_bar in qqq_bars
            ),
        }
    )
    market_data = StaticMarketData(
        {
            daily_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, daily_bar)
            for daily_bar in qqq_bars
        }
    )

    assert (
        evaluate_metric(calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 6), metric)
        == expected
    )


@pytest.mark.parametrize(
    "metric",
    [
        MetricReference(AssetSymbol.QQQ, MetricType.SIMPLE, 1),
        MetricReference(AssetSymbol.QQQ, MetricType.RETURN, None),
        MetricReference(AssetSymbol.QQQ, MetricType.CHANGE, 0),
    ],
)
def test_metric_dispatch_preserves_invalid_metric_rejection(metric: MetricReference) -> None:
    with pytest.raises(ValueError):
        evaluate_metric(
            StaticCalendar({}), StaticMarketData({}), AssetSymbol.QQQ, date(2025, 1, 3), metric
        )


@pytest.mark.parametrize(
    ("metric_type", "expected"),
    [
        (MetricType.RETURN, Decimal("0.21")),
        (MetricType.CHANGE, Decimal("21")),
    ],
)
def test_lookback_metric_uses_exact_referenced_asset_sessions(
    metric_type: MetricType, expected: Decimal
) -> None:
    qqq_bars = [
        bar("QQQ", date(2025, 1, 2), "100"),
        bar("QQQ", date(2025, 1, 3), "110"),
        bar("QQQ", date(2025, 1, 6), "121"),
    ]
    calendar = StaticCalendar(
        {
            "QQQ": tuple(
                completion(
                    "QQQ",
                    daily_bar.session_date,
                    datetime.combine(daily_bar.session_date, datetime.min.time(), UTC),
                )
                for daily_bar in qqq_bars
            ),
        }
    )
    market_data = StaticMarketData(
        {
            daily_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, daily_bar)
            for daily_bar in qqq_bars
        }
    )
    assert (
        evaluate_lookback_metric(
            calendar,
            market_data,
            AssetSymbol.QQQ,
            date(2025, 1, 6),
            lookback_metric(AssetSymbol.QQQ, metric_type, 2),
        )
        == expected
    )


def test_cross_calendar_lookback_stays_on_the_referenced_asset_calendar() -> None:
    qqq_bars = [bar("QQQ", date(2025, 1, 2), "100"), bar("QQQ", date(2025, 1, 3), "110")]
    calendar = StaticCalendar(
        {
            "BTCUSDT": (completion("BTCUSDT", date(2025, 1, 4), datetime(2025, 1, 5, tzinfo=UTC)),),
            "QQQ": (
                completion("QQQ", date(2025, 1, 2), datetime(2025, 1, 2, 21, tzinfo=UTC)),
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {
            daily_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, daily_bar)
            for daily_bar in qqq_bars
        }
    )
    assert evaluate_lookback_metric(
        calendar,
        market_data,
        AssetSymbol.BTCUSDT,
        date(2025, 1, 4),
        lookback_metric(AssetSymbol.QQQ, MetricType.RETURN, 1),
    ) == Decimal("0.1")


def test_lookback_history_outside_the_snapshot_fails_fast() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "110")
    calendar = StaticCalendar(
        {
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )
    market_data = StaticMarketData(
        {qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar)}
    )
    with pytest.raises(TemporalResolutionError) as error:
        evaluate_lookback_metric(
            calendar,
            market_data,
            AssetSymbol.QQQ,
            date(2025, 1, 3),
            lookback_metric(AssetSymbol.QQQ, MetricType.RETURN, 1),
        )
    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"


def test_missing_lookback_daily_bar_fails_fast() -> None:
    anchor_bar = bar("QQQ", date(2025, 1, 3), "110")
    calendar = StaticCalendar(
        {
            "QQQ": (
                completion("QQQ", date(2025, 1, 2), datetime(2025, 1, 2, 21, tzinfo=UTC)),
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {
            AssetSession("QQQ", date(2025, 1, 2)): DailyBarLookup(
                DailyBarAvailability.MISSING_EXPECTED_SESSION
            ),
            anchor_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, anchor_bar),
        }
    )
    with pytest.raises(MissingRequiredMarketDataError) as error:
        evaluate_lookback_metric(
            calendar,
            market_data,
            AssetSymbol.QQQ,
            date(2025, 1, 3),
            lookback_metric(AssetSymbol.QQQ, MetricType.CHANGE, 1),
        )
    assert error.value.error_code == "MISSING_REQUIRED_DATA"


def test_return_with_non_positive_lookback_close_is_dataset_corruption() -> None:
    anchor_bar = bar("QQQ", date(2025, 1, 3), "110")
    lookback_bar = bar("QQQ", date(2025, 1, 2), "0")
    calendar = StaticCalendar(
        {
            "QQQ": (
                completion("QQQ", date(2025, 1, 2), datetime(2025, 1, 2, 21, tzinfo=UTC)),
                completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),
            ),
        }
    )
    market_data = StaticMarketData(
        {
            lookback_bar.asset_session: DailyBarLookup(
                DailyBarAvailability.AVAILABLE, lookback_bar
            ),
            anchor_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, anchor_bar),
        }
    )
    with pytest.raises(DatasetCorruptionError) as error:
        evaluate_lookback_metric(
            calendar,
            market_data,
            AssetSymbol.QQQ,
            date(2025, 1, 3),
            lookback_metric(AssetSymbol.QQQ, MetricType.RETURN, 1),
        )
    assert error.value.error_code == "DATASET_CORRUPTION"
