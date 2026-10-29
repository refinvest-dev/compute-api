"""Build deterministic Execution Asset equity curves and performance metrics."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.trades import Trade
from refinvest.engine.validation.models import AssetSymbol, FeeModel, Period
from refinvest.marketdata.domain import AssetCalendar, calendar_for_asset
from refinvest.marketdata.ports import (
    DatasetCorruptionError,
    MarketDataPort,
    SessionCalendarPort,
    require_daily_bar,
)


@dataclass(frozen=True)
class EquityCurvePoint:
    session_date: date
    value: Decimal


@dataclass(frozen=True)
class PerformanceMetrics:
    total_return: Decimal | None
    cagr: Decimal | None
    mdd: Decimal | None
    sharpe: Decimal | None
    win_rate: Decimal | None
    trade_count: int
    avg_trade_return: Decimal | None
    avg_holding_period: Decimal | None
    profit_factor: Decimal | None


@dataclass(frozen=True)
class PerformanceReport:
    equity_curve: tuple[EquityCurvePoint, ...]
    metrics: PerformanceMetrics


def evaluate_performance(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    fee_model: FeeModel,
    actual_period: Period,
    trades: tuple[Trade, ...],
) -> PerformanceReport:
    """Evaluate realized trades on the pinned Execution Asset Daily Bar sequence."""
    _require_fee_model(fee_model)
    sessions = _expected_sessions_in_period(calendar, execution_asset, actual_period)
    _validate_trades(trades, sessions)
    curve = _build_equity_curve(
        market_data, execution_asset, fee_model.commission, sessions, trades
    )
    return PerformanceReport(curve, _calculate_metrics(curve, trades, execution_asset))


def _expected_sessions_in_period(
    calendar: SessionCalendarPort, execution_asset: AssetSymbol, actual_period: Period
) -> tuple[date, ...]:
    sessions = calendar.expected_sessions(execution_asset.value)
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError("Execution Asset expected sessions must be ordered and unique.")
    scoped_sessions = tuple(
        session for session in sessions if actual_period.start <= session <= actual_period.end
    )
    if not scoped_sessions:
        raise ValueError("The actual period contains no Execution Asset expected session.")
    return scoped_sessions


def _validate_trades(trades: tuple[Trade, ...], sessions: tuple[date, ...]) -> None:
    if any(
        earlier.entry_session >= later.entry_session
        for earlier, later in zip(trades, trades[1:], strict=False)
    ):
        raise ValueError("Trades must be ordered by strictly ascending entry session.")
    if any(trade.entry_session >= trade.exit_session for trade in trades):
        raise ValueError("A trade exit session must be later than its entry session.")
    session_set = frozenset(sessions)
    if any(
        trade.entry_session not in session_set or trade.exit_session not in session_set
        for trade in trades
    ):
        raise ValueError("Every trade execution session must be inside the actual period.")
    if any(
        later.entry_session <= earlier.exit_session
        for earlier, later in zip(trades, trades[1:], strict=False)
    ):
        raise ValueError("Trades must not overlap or share an execution session.")


def _build_equity_curve(
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    commission: Decimal,
    sessions: tuple[date, ...],
    trades: tuple[Trade, ...],
) -> tuple[EquityCurvePoint, ...]:
    entries = {trade.entry_session: trade for trade in trades}
    exits = {trade.exit_session: trade for trade in trades}
    equity = Decimal(1)
    units: Decimal | None = None
    active_trade: Trade | None = None
    curve: list[EquityCurvePoint] = []

    for session in sessions:
        bar = require_daily_bar(market_data.get_daily_bar(execution_asset.value, session))
        if bar.close <= 0:
            raise DatasetCorruptionError("Daily Bar close must be positive for equity valuation.")

        exiting_trade = exits.get(session)
        if exiting_trade is not None:
            if active_trade != exiting_trade or units is None:
                raise ValueError("An exit must close the currently active trade.")
            with localcontext(EXECUTION_DECIMAL_CONTEXT):
                equity = units * exiting_trade.exit_proceeds
            active_trade = None
            units = None

        entering_trade = entries.get(session)
        if entering_trade is not None:
            if active_trade is not None:
                raise ValueError("An entry cannot occur while another trade is active.")
            with localcontext(EXECUTION_DECIMAL_CONTEXT):
                units = equity / entering_trade.entry_cost
            active_trade = entering_trade

        if active_trade is not None:
            assert units is not None
            with localcontext(EXECUTION_DECIMAL_CONTEXT):
                equity = units * bar.close * (Decimal(1) - commission)
        curve.append(EquityCurvePoint(session, equity))

    if active_trade is not None:
        raise ValueError("The actual period ends with an unrealized active trade.")
    return tuple(curve)


def _calculate_metrics(
    curve: tuple[EquityCurvePoint, ...],
    trades: tuple[Trade, ...],
    execution_asset: AssetSymbol,
) -> PerformanceMetrics:
    if not trades:
        return PerformanceMetrics(None, None, None, None, None, 0, None, None, None)

    values = tuple(point.value for point in curve)
    total_return = values[-1] - Decimal(1)
    return PerformanceMetrics(
        total_return=total_return,
        cagr=_cagr(values[-1], curve[0].session_date, curve[-1].session_date),
        mdd=_maximum_drawdown(values),
        sharpe=_sharpe(values, execution_asset),
        win_rate=_win_rate(trades),
        trade_count=len(trades),
        avg_trade_return=_mean(tuple(trade.return_pct for trade in trades)),
        avg_holding_period=_mean(tuple(Decimal(trade.holding_signal_sessions) for trade in trades)),
        profit_factor=_profit_factor(trades),
    )


def _cagr(final_equity: Decimal, start: date, end: date) -> Decimal | None:
    calendar_days = (end - start).days
    if calendar_days == 0:
        return None
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        exponent = Decimal("365.25") / Decimal(calendar_days)
        return (final_equity.ln() * exponent).exp() - Decimal(1)


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    peak = Decimal(1)
    drawdowns: list[Decimal] = []
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        for value in values:
            peak = max(peak, value)
            drawdowns.append((value / peak) - Decimal(1))
    return min(drawdowns)


def _sharpe(values: tuple[Decimal, ...], execution_asset: AssetSymbol) -> Decimal | None:
    if len(values) < 2:
        return None
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        returns = tuple(
            (current / previous) - Decimal(1)
            for previous, current in zip(values, values[1:], strict=False)
        )
        average = _mean(returns)
        deviation = _sample_standard_deviation(returns, average)
        if deviation is None or deviation == 0:
            return None
        annualization = Decimal(252)
        if calendar_for_asset(execution_asset.value) is AssetCalendar.CRYPTO_UTC:
            annualization = Decimal(365)
        return (average / deviation) * annualization.sqrt()


def _win_rate(trades: tuple[Trade, ...]) -> Decimal:
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        return Decimal(sum(trade.return_pct > 0 for trade in trades)) / Decimal(len(trades))


def _profit_factor(trades: tuple[Trade, ...]) -> Decimal | None:
    capital = Decimal(1)
    gross_profit = Decimal(0)
    gross_loss = Decimal(0)
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        for trade in trades:
            pnl = capital * trade.return_pct
            if pnl > 0:
                gross_profit += pnl
            elif pnl < 0:
                gross_loss -= pnl
            capital += pnl
        if gross_loss == 0:
            return None
        return gross_profit / gross_loss


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        return sum(values, Decimal(0)) / Decimal(len(values))


def _sample_standard_deviation(values: tuple[Decimal, ...], average: Decimal) -> Decimal | None:
    if len(values) < 2:
        return None
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        variance = sum(((value - average) ** 2 for value in values), Decimal(0)) / Decimal(
            len(values) - 1
        )
        return variance.sqrt()


def _require_fee_model(fee_model: FeeModel) -> None:
    if not Decimal(0) <= fee_model.commission < Decimal(1):
        raise ValueError("commission must be in [0, 1).")
    if not Decimal(0) <= fee_model.slippage < Decimal(1):
        raise ValueError("slippage must be in [0, 1).")
