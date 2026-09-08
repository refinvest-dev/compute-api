from datetime import date
from decimal import Decimal, localcontext

import pytest

from refinvest.engine.benchmark import (
    evaluate_benchmark_references,
    evaluate_buy_and_hold_reference,
)
from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.validation.models import AssetSymbol, Period
from refinvest.marketdata.domain import (
    AssetSession,
    ClosePriceLookup,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)
from refinvest.marketdata.ports import DatasetCorruptionError, MissingRequiredMarketDataError


class StaticCalendar:
    def __init__(self, sessions: dict[str, tuple[date, ...]]) -> None:
        self.sessions = sessions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return self.sessions[asset]

    def expected_session_completions(self, asset: str) -> tuple[object, ...]:
        return ()


class StaticMarketData:
    def __init__(self, bars: dict[AssetSession, DailyBarLookup]) -> None:
        self.bars = bars

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        return self.bars[AssetSession(asset, session_date)]

    def get_close(self, asset: str, session_date: date) -> ClosePriceLookup:
        lookup = self.get_daily_bar(asset, session_date)
        return ClosePriceLookup(
            lookup.availability, lookup.bar.close if lookup.bar is not None else None
        )


def period() -> Period:
    return Period(date(2025, 1, 2), date(2025, 1, 7))


def daily_bar(asset: AssetSymbol, session: date, close: str) -> DailyBarLookup:
    price = Decimal(close)
    return DailyBarLookup(
        DailyBarAvailability.AVAILABLE,
        DailyBar(asset.value, session, price, price, price, price, 1),
    )


def calendar() -> StaticCalendar:
    return StaticCalendar(
        {
            "TQQQ": (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)),
            "QQQ": (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)),
        }
    )


def market_data() -> StaticMarketData:
    sessions = calendar().expected_sessions("TQQQ")
    tqqq_closes = ("100", "120", "90", "110")
    qqq_closes = ("20", "25", "15", "30")
    return StaticMarketData(
        {
            **{
                AssetSession("TQQQ", session): daily_bar(AssetSymbol.TQQQ, session, close)
                for session, close in zip(sessions, tqqq_closes, strict=True)
            },
            **{
                AssetSession("QQQ", session): daily_bar(AssetSymbol.QQQ, session, close)
                for session, close in zip(sessions, qqq_closes, strict=True)
            },
        }
    )


def test_builds_cost_free_close_to_close_reference_metrics() -> None:
    result = evaluate_buy_and_hold_reference(calendar(), market_data(), AssetSymbol.TQQQ, period())

    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        expected_cagr = (Decimal("1.1").ln() * (Decimal("365.25") / Decimal(5))).exp() - Decimal(1)
    assert tuple(point.value for point in result.equity_curve) == (
        Decimal("1"),
        Decimal("1.2"),
        Decimal("0.9"),
        Decimal("1.1"),
    )
    assert result.total_return == Decimal("0.1")
    assert result.mdd == Decimal("-0.25")
    assert result.cagr == expected_cagr


def test_qqq_is_a_secondary_reference_for_same_market_different_asset_strategy() -> None:
    result = evaluate_benchmark_references(
        calendar(), market_data(), AssetSymbol.TQQQ, AssetSymbol.QQQ, period()
    )

    assert result.primary.asset is AssetSymbol.TQQQ
    assert result.secondary_reference is not None
    assert result.secondary_reference.asset is AssetSymbol.QQQ
    assert result.secondary_reference.total_return == Decimal("0.5")


def test_same_asset_strategy_omits_secondary_reference() -> None:
    result = evaluate_benchmark_references(
        calendar(), market_data(), AssetSymbol.TQQQ, AssetSymbol.TQQQ, period()
    )

    assert result.secondary_reference is None


def test_missing_expected_benchmark_bar_fails_fast() -> None:
    bars = market_data().bars
    missing = AssetSession("TQQQ", date(2025, 1, 3))
    bars[missing] = DailyBarLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)

    with pytest.raises(MissingRequiredMarketDataError):
        evaluate_buy_and_hold_reference(
            calendar(), StaticMarketData(bars), AssetSymbol.TQQQ, period()
        )


def test_non_positive_close_is_dataset_corruption() -> None:
    bars = market_data().bars
    invalid = AssetSession("TQQQ", date(2025, 1, 3))
    bars[invalid] = daily_bar(AssetSymbol.TQQQ, invalid.session_date, "0")

    with pytest.raises(DatasetCorruptionError):
        evaluate_buy_and_hold_reference(
            calendar(), StaticMarketData(bars), AssetSymbol.TQQQ, period()
        )


def test_period_without_asset_session_is_calendar_resolution_failure() -> None:
    with pytest.raises(TemporalResolutionError) as error:
        evaluate_buy_and_hold_reference(
            calendar(),
            market_data(),
            AssetSymbol.TQQQ,
            Period(date(2025, 2, 1), date(2025, 2, 2)),
        )

    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"


def test_benchmark_evaluation_is_deterministic() -> None:
    inputs = (calendar(), market_data(), AssetSymbol.TQQQ, AssetSymbol.QQQ, period())

    assert evaluate_benchmark_references(*inputs) == evaluate_benchmark_references(*inputs)
