"""Resolve entry execution sessions and prices from pinned snapshot data."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from refinvest.engine.entries import EntryCandidate
from refinvest.engine.execution.schedule import EntryExecutionSchedule
from refinvest.engine.temporal import resolve_next_execution_session
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.ports import MarketDataPort, SessionCalendarPort, require_daily_bar

EXECUTION_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class EntryExecution:
    """A long entry prepared at the execution session open with buy-side slippage."""

    candidate: EntryCandidate
    execution_asset: AssetSymbol
    execution_session: date
    reference_price: Decimal
    entry_price: Decimal


def prepare_entry_executions(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    slippage: Decimal,
    candidates: tuple[EntryCandidate, ...],
) -> tuple[EntryExecution, ...]:
    """Resolve next execution sessions and open-based long-entry prices."""
    return tuple(
        _prepare_entry_execution(calendar, market_data, execution_asset, slippage, candidate)
        for candidate in candidates
    )


def price_entry_execution_schedules(
    market_data: MarketDataPort,
    slippage: Decimal,
    schedules: tuple[EntryExecutionSchedule, ...],
) -> tuple[EntryExecution, ...]:
    """Price only schedules admitted by the single-position policy."""
    return tuple(
        _price_entry_execution(market_data, slippage, schedule.candidate, schedule.execution_asset,
                               schedule.execution_session)
        for schedule in schedules
    )


def _prepare_entry_execution(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    execution_asset: AssetSymbol,
    slippage: Decimal,
    candidate: EntryCandidate,
) -> EntryExecution:
    execution_session = resolve_next_execution_session(
        calendar, execution_asset.value, candidate.entry_signal_session
    )
    return _price_entry_execution(
        market_data, slippage, candidate, execution_asset, execution_session
    )


def _price_entry_execution(
    market_data: MarketDataPort,
    slippage: Decimal,
    candidate: EntryCandidate,
    execution_asset: AssetSymbol,
    execution_session: date,
) -> EntryExecution:
    reference_price = require_daily_bar(
        market_data.get_daily_bar(execution_asset.value, execution_session)
    ).open
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        entry_price = reference_price * (Decimal(1) + slippage)
    return EntryExecution(
        candidate, execution_asset, execution_session, reference_price, entry_price
    )
