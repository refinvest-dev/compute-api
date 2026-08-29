from datetime import date
from decimal import Decimal, localcontext

import pytest

from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.performance import evaluate_performance
from refinvest.engine.trades import Trade
from refinvest.engine.validation.models import AssetSymbol, FeeModel, Period
from refinvest.marketdata.domain import AssetSession, DailyBar, DailyBarAvailability, DailyBarLookup
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


def period() -> Period:
    return Period(date(2025, 1, 2), date(2025, 1, 7))


def sessions() -> tuple[date, ...]:
    return (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7))


def calendar(asset: AssetSymbol = AssetSymbol.TQQQ) -> StaticCalendar:
    return StaticCalendar({asset.value: sessions()})


def market_data(
    closes: tuple[str, ...] = ("100", "110", "110", "120"),
    asset: AssetSymbol = AssetSymbol.TQQQ,
) -> StaticMarketData:
    return StaticMarketData(
        {
            AssetSession(asset.value, session): DailyBarLookup(
                DailyBarAvailability.AVAILABLE,
                DailyBar(asset.value, session, Decimal(close), Decimal(close), Decimal(close),
                         Decimal(close), 1),
            )
            for session, close in zip(sessions(), closes, strict=True)
        }
    )


def trade(
    entry_session: date = date(2025, 1, 2),
    exit_session: date = date(2025, 1, 6),
    entry_cost: Decimal = Decimal("101"),
    exit_proceeds: Decimal = Decimal("108.9"),
    holding_signal_sessions: int = 2,
) -> Trade:
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        return_pct = (exit_proceeds / entry_cost) - Decimal(1)
    return Trade(
        signal_session=date(2025, 1, 1),
        entry_session=entry_session,
        entry_price=Decimal("100"),
        entry_cost=entry_cost,
        exit_session=exit_session,
        exit_price=Decimal("110"),
        exit_proceeds=exit_proceeds,
        return_pct=return_pct,
        holding_signal_sessions=holding_signal_sessions,
    )


def fee_model(commission: str = "0.01") -> FeeModel:
    return FeeModel(commission=Decimal(commission), slippage=Decimal("0"))


def test_builds_close_marked_curve_and_metrics_from_realized_trade() -> None:
    result = evaluate_performance(
        calendar(), market_data(), AssetSymbol.TQQQ, fee_model(), period(), (trade(),)
    )

    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        entry_close_value = (Decimal(100) / Decimal(101)) * Decimal("0.99")
        final_value = Decimal("108.9") / Decimal(101)
        expected_cagr = (
            (final_value.ln() * (Decimal("365.25") / Decimal(5))).exp() - Decimal(1)
        )
    assert result.equity_curve == (
        result.equity_curve[0].__class__(date(2025, 1, 2), entry_close_value),
        result.equity_curve[1].__class__(date(2025, 1, 3), final_value),
        result.equity_curve[2].__class__(date(2025, 1, 6), final_value),
        result.equity_curve[3].__class__(date(2025, 1, 7), final_value),
    )
    assert result.metrics.total_return == final_value - Decimal(1)
    assert result.metrics.mdd == entry_close_value - Decimal(1)
    assert result.metrics.cagr == expected_cagr
    assert result.metrics.trade_count == 1
    assert result.metrics.win_rate == Decimal(1)
    assert result.metrics.avg_holding_period == Decimal(2)
    assert result.metrics.profit_factor is None


def test_profit_factor_uses_sequential_normalized_realized_pnl() -> None:
    first_trade = trade(
        date(2025, 1, 2), date(2025, 1, 3), Decimal("100"), Decimal("110"), 1
    )
    second_trade = trade(
        date(2025, 1, 6), date(2025, 1, 7), Decimal("100"), Decimal("90"), 1
    )
    result = evaluate_performance(
        calendar(), market_data(), AssetSymbol.TQQQ, fee_model("0"), period(),
        (first_trade, second_trade),
    )

    assert result.metrics.total_return == Decimal("-0.01")
    assert result.metrics.win_rate == Decimal("0.5")
    assert result.metrics.avg_trade_return == Decimal("0")
    assert result.metrics.profit_factor == Decimal("0.1") / Decimal("0.11")


def test_zero_trade_preserves_empty_metric_semantics() -> None:
    result = evaluate_performance(
        calendar(), market_data(), AssetSymbol.TQQQ, fee_model(), period(), ()
    )

    assert tuple(point.value for point in result.equity_curve) == (Decimal(1),) * 4
    assert result.metrics.trade_count == 0
    assert result.metrics.total_return is None
    assert result.metrics.sharpe is None
    assert result.metrics.win_rate is None


def test_expected_session_gap_fails_fast_even_while_flat() -> None:
    bars = market_data().bars
    missing = AssetSession(AssetSymbol.TQQQ.value, date(2025, 1, 3))
    bars[missing] = DailyBarLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)

    with pytest.raises(MissingRequiredMarketDataError):
        evaluate_performance(
            calendar(), StaticMarketData(bars), AssetSymbol.TQQQ, fee_model(), period(), ()
        )


def test_non_positive_close_is_dataset_corruption() -> None:
    with pytest.raises(DatasetCorruptionError):
        evaluate_performance(
            calendar(), market_data(("0", "110", "110", "120")), AssetSymbol.TQQQ,
            fee_model(), period(), (),
        )


def test_sharpe_uses_execution_asset_calendar_annualization() -> None:
    result_equity = evaluate_performance(
        calendar(AssetSymbol.QQQ), market_data(asset=AssetSymbol.QQQ), AssetSymbol.QQQ,
        fee_model("0"), period(), (trade(),),
    )
    result_crypto = evaluate_performance(
        calendar(AssetSymbol.BTCUSDT), market_data(asset=AssetSymbol.BTCUSDT), AssetSymbol.BTCUSDT,
        fee_model("0"), period(), (trade(),),
    )

    assert result_equity.metrics.sharpe is not None
    assert result_crypto.metrics.sharpe is not None
    assert result_crypto.metrics.sharpe > result_equity.metrics.sharpe


def test_performance_evaluation_is_deterministic() -> None:
    inputs = (calendar(), market_data(), AssetSymbol.TQQQ, fee_model(), period(), (trade(),))

    assert evaluate_performance(*inputs) == evaluate_performance(*inputs)
