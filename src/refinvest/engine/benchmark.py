"""Build deterministic cost-free market references from pinned Daily Bar closes."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.validation.models import AssetSymbol, Period
from refinvest.marketdata.ports import (
    DatasetCorruptionError,
    MarketDataPort,
    SessionCalendarPort,
    require_close,
)


@dataclass(frozen=True)
class BenchmarkCurvePoint:
    session_date: date
    value: Decimal


@dataclass(frozen=True)
class BuyAndHoldReference:
    asset: AssetSymbol
    equity_curve: tuple[BenchmarkCurvePoint, ...]
    total_return: Decimal
    cagr: Decimal | None
    mdd: Decimal


@dataclass(frozen=True)
class BenchmarkReferences:
    primary: BuyAndHoldReference
    secondary_reference: BuyAndHoldReference | None


def evaluate_benchmark_references(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    primary_signal_asset: AssetSymbol,
    actual_period: Period,
) -> BenchmarkReferences:
    """Return the execution benchmark and, for cross-market strategies, signal reference."""
    primary = evaluate_buy_and_hold_reference(calendar, market_data, execution_asset, actual_period)
    secondary_reference = None
    if primary_signal_asset is not execution_asset:
        secondary_reference = evaluate_buy_and_hold_reference(
            calendar, market_data, primary_signal_asset, actual_period
        )
    return BenchmarkReferences(primary, secondary_reference)


def evaluate_buy_and_hold_reference(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    asset: AssetSymbol,
    actual_period: Period,
) -> BuyAndHoldReference:
    """Evaluate a cost-free normalized Close-to-Close market reference for one asset."""
    sessions = _expected_sessions_in_period(calendar, asset, actual_period)
    closes = tuple(_require_positive_close(market_data, asset, session) for session in sessions)
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        curve = tuple(
            BenchmarkCurvePoint(session, close / closes[0])
            for session, close in zip(sessions, closes, strict=True)
        )
        total_return = curve[-1].value - Decimal(1)
    return BuyAndHoldReference(
        asset=asset,
        equity_curve=curve,
        total_return=total_return,
        cagr=_cagr(curve[-1].value, curve[0].session_date, curve[-1].session_date),
        mdd=_maximum_drawdown(tuple(point.value for point in curve)),
    )


def _expected_sessions_in_period(
    calendar: SessionCalendarPort, asset: AssetSymbol, actual_period: Period
) -> tuple[date, ...]:
    sessions = calendar.expected_sessions(asset.value)
    if sessions != tuple(sorted(set(sessions))):
        raise TemporalResolutionError("Benchmark expected sessions must be ordered and unique.")
    scoped_sessions = tuple(
        session for session in sessions if actual_period.start <= session <= actual_period.end
    )
    if not scoped_sessions:
        raise TemporalResolutionError("The actual period has no benchmark session.")
    return scoped_sessions


def _require_positive_close(
    market_data: MarketDataPort, asset: AssetSymbol, session: date
) -> Decimal:
    close = require_close(market_data.get_close(asset.value, session))
    if close <= 0:
        raise DatasetCorruptionError("Daily Bar close must be positive for benchmark valuation.")
    return close


def _cagr(final_value: Decimal, start: date, end: date) -> Decimal | None:
    calendar_days = (end - start).days
    if calendar_days == 0:
        return None
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        exponent = Decimal("365.25") / Decimal(calendar_days)
        return (final_value.ln() * exponent).exp() - Decimal(1)


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    peak = Decimal(1)
    drawdowns: list[Decimal] = []
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        for value in values:
            peak = max(peak, value)
            drawdowns.append((value / peak) - Decimal(1))
    return min(drawdowns)
