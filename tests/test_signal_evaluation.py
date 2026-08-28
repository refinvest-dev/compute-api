from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.engine.signals import Signal, evaluate_signals
from refinvest.engine.validation.models import (
    AssetSymbol,
    ComparisonOperator,
    Condition,
    MetricReference,
    MetricType,
    StrategyVersion,
)
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


def simple(asset: AssetSymbol) -> MetricReference:
    return MetricReference(asset, MetricType.SIMPLE, None)


def strategy(primary_signal_asset: AssetSymbol, condition: Condition) -> StrategyVersion:
    return StrategyVersion(
        primary_signal_asset,
        (condition,),
        AssetSymbol.TQQQ,
        0,
        1,
        None,
    )


def test_emits_only_sessions_with_a_true_condition_result() -> None:
    qqq_bars = [bar("QQQ", date(2025, 1, 2), "90"), bar("QQQ", date(2025, 1, 3), "110")]
    calendar = StaticCalendar(
        {
            "QQQ": tuple(
                completion("QQQ", daily_bar.session_date, datetime.combine(
                    daily_bar.session_date, datetime.min.time(), UTC
                ))
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
    tested_strategy = strategy(
        AssetSymbol.QQQ,
        Condition(ComparisonOperator.GT, None, simple(AssetSymbol.QQQ), Decimal("100")),
    )

    assert evaluate_signals(
        calendar,
        market_data,
        tested_strategy,
        (date(2025, 1, 2), date(2025, 1, 3)),
    ) == (Signal(AssetSymbol.QQQ, date(2025, 1, 3)),)


def test_returns_no_signals_when_no_session_matches() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "90")
    calendar = StaticCalendar(
        {"QQQ": (completion("QQQ", qqq_bar.session_date, datetime(2025, 1, 3, 21, tzinfo=UTC)),)}
    )
    market_data = StaticMarketData(
        {qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar)}
    )
    tested_strategy = strategy(
        AssetSymbol.QQQ,
        Condition(ComparisonOperator.GT, None, simple(AssetSymbol.QQQ), Decimal("100")),
    )

    assert evaluate_signals(calendar, market_data, tested_strategy, (qqq_bar.session_date,)) == ()


def test_uses_primary_signal_sessions_when_conditions_reference_another_asset() -> None:
    vix_bar = bar("VIX", date(2025, 1, 3), "18")
    signal_sessions = (date(2025, 1, 4), date(2025, 1, 5))
    calendar = StaticCalendar(
        {
            "BTCUSDT": (
                completion("BTCUSDT", signal_sessions[0], datetime(2025, 1, 5, tzinfo=UTC)),
                completion("BTCUSDT", signal_sessions[1], datetime(2025, 1, 6, tzinfo=UTC)),
            ),
            "VIX": (completion("VIX", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )
    market_data = StaticMarketData(
        {vix_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, vix_bar)}
    )
    tested_strategy = strategy(
        AssetSymbol.BTCUSDT,
        Condition(ComparisonOperator.LT, None, simple(AssetSymbol.VIX), Decimal("20")),
    )

    assert evaluate_signals(calendar, market_data, tested_strategy, signal_sessions) == (
        Signal(AssetSymbol.BTCUSDT, date(2025, 1, 4)),
        Signal(AssetSymbol.BTCUSDT, date(2025, 1, 5)),
    )


def test_propagates_missing_required_data_without_skipping_a_signal_session() -> None:
    calendar = StaticCalendar(
        {"QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),)}
    )
    market_data = StaticMarketData(
        {
            AssetSession("QQQ", date(2025, 1, 3)): DailyBarLookup(
                DailyBarAvailability.MISSING_EXPECTED_SESSION
            )
        }
    )
    tested_strategy = strategy(
        AssetSymbol.QQQ,
        Condition(ComparisonOperator.GT, None, simple(AssetSymbol.QQQ), Decimal("0")),
    )

    with pytest.raises(MissingRequiredMarketDataError):
        evaluate_signals(calendar, market_data, tested_strategy, (date(2025, 1, 3),))


def test_rejects_non_ascending_or_duplicate_signal_sessions() -> None:
    tested_strategy = strategy(
        AssetSymbol.QQQ,
        Condition(ComparisonOperator.GT, None, simple(AssetSymbol.QQQ), Decimal("0")),
    )

    with pytest.raises(ValueError, match="strictly ascending"):
        evaluate_signals(
            StaticCalendar({}),
            StaticMarketData({}),
            tested_strategy,
            (date(2025, 1, 3), date(2025, 1, 3)),
        )


def test_signal_evaluation_is_deterministic_for_the_same_snapshot_inputs() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "100")
    calendar = StaticCalendar(
        {"QQQ": (completion("QQQ", qqq_bar.session_date, datetime(2025, 1, 3, 21, tzinfo=UTC)),)}
    )
    market_data = StaticMarketData(
        {qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar)}
    )
    tested_strategy = strategy(
        AssetSymbol.QQQ,
        Condition(ComparisonOperator.GTE, None, simple(AssetSymbol.QQQ), Decimal("100")),
    )

    assert [
        evaluate_signals(calendar, market_data, tested_strategy, (qqq_bar.session_date,))
        for _ in range(2)
    ] == [
        (Signal(AssetSymbol.QQQ, date(2025, 1, 3)),),
        (Signal(AssetSymbol.QQQ, date(2025, 1, 3)),),
    ]
