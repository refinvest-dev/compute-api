"""Resolve long-exit execution sessions and sell prices from pinned snapshot data."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from refinvest.engine.entries import ExitCandidate
from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.execution.schedule import ExitExecutionSchedule
from refinvest.engine.temporal import resolve_next_execution_session
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.ports import MarketDataPort, SessionCalendarPort, require_daily_bar


@dataclass(frozen=True)
class ExitExecution:
    candidate: ExitCandidate
    execution_asset: AssetSymbol
    execution_session: date
    reference_price: Decimal
    exit_price: Decimal


def prepare_exit_executions(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    slippage: Decimal,
    candidates: tuple[ExitCandidate, ...],
) -> tuple[ExitExecution, ...]:
    """Resolve next execution sessions and open-based long-exit prices."""
    return tuple(
        _prepare_exit_execution(calendar, market_data, execution_asset, slippage, candidate)
        for candidate in candidates
    )


def price_exit_execution_schedules(
    market_data: MarketDataPort,
    slippage: Decimal,
    schedules: tuple[ExitExecutionSchedule, ...],
) -> tuple[ExitExecution, ...]:
    """Price only exit schedules belonging to admitted positions."""
    return tuple(
        _price_exit_execution(market_data, slippage, schedule.candidate, schedule.execution_asset,
                              schedule.execution_session)
        for schedule in schedules
    )


def _prepare_exit_execution(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    slippage: Decimal,
    candidate: ExitCandidate,
) -> ExitExecution:
    execution_session = resolve_next_execution_session(
        calendar, execution_asset.value, candidate.exit_signal_session
    )
    return _price_exit_execution(
        market_data, slippage, candidate, execution_asset, execution_session
    )


def _price_exit_execution(
    market_data: MarketDataPort,
    slippage: Decimal,
    candidate: ExitCandidate,
    execution_asset: AssetSymbol,
    execution_session: date,
) -> ExitExecution:
    reference_price = require_daily_bar(
        market_data.get_daily_bar(execution_asset.value, execution_session)
    ).open
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        exit_price = reference_price * (Decimal(1) - slippage)
    return ExitExecution(candidate, execution_asset, execution_session, reference_price, exit_price)
