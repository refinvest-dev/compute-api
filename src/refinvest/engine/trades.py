"""Realize admitted long positions into deterministic trade ledger entries."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from refinvest.engine.execution import EntryExecution, ExitExecution
from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.position import PlannedPosition
from refinvest.engine.validation.models import FeeModel


class SampleSizeWarning(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    ZERO = "ZERO"


@dataclass(frozen=True)
class Trade:
    """A completed long trade with slippage and both-side commission applied."""

    signal_session: date
    entry_session: date
    entry_price: Decimal
    entry_cost: Decimal
    exit_session: date
    exit_price: Decimal
    exit_proceeds: Decimal
    return_pct: Decimal
    holding_signal_sessions: int


def realize_trades(
    fee_model: FeeModel,
    positions: tuple[PlannedPosition, ...],
    entries: tuple[EntryExecution, ...],
    exits: tuple[ExitExecution, ...],
    holding_signal_sessions: int,
) -> tuple[Trade, ...]:
    """Realize exactly the priced executions that belong to admitted positions."""
    _require_fee_model(fee_model)
    if holding_signal_sessions <= 0:
        raise ValueError("holding_signal_sessions must be positive.")
    if len(positions) != len(entries) or len(positions) != len(exits):
        raise ValueError("Every admitted position must have exactly one entry and exit execution.")

    return tuple(
        _realize_trade(fee_model.commission, position, entry, exit, holding_signal_sessions)
        for position, entry, exit in zip(positions, entries, exits, strict=True)
    )


def sample_size_warning(trades: tuple[Trade, ...]) -> SampleSizeWarning:
    """Classify realized trade counts without fabricating empty-state statistics."""
    if not trades:
        return SampleSizeWarning.ZERO
    if len(trades) < 10:
        return SampleSizeWarning.LOW
    return SampleSizeWarning.NONE


def _realize_trade(
    commission: Decimal,
    position: PlannedPosition,
    entry: EntryExecution,
    exit: ExitExecution,
    holding_signal_sessions: int,
) -> Trade:
    _require_matching_executions(position, entry, exit)
    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        entry_cost = entry.entry_price * (Decimal(1) + commission)
        exit_proceeds = exit.exit_price * (Decimal(1) - commission)
        return_pct = (exit_proceeds / entry_cost) - Decimal(1)
    return Trade(
        signal_session=entry.candidate.signal_session,
        entry_session=entry.execution_session,
        entry_price=entry.entry_price,
        entry_cost=entry_cost,
        exit_session=exit.execution_session,
        exit_price=exit.exit_price,
        exit_proceeds=exit_proceeds,
        return_pct=return_pct,
        holding_signal_sessions=holding_signal_sessions,
    )


def _require_fee_model(fee_model: FeeModel) -> None:
    if not Decimal(0) <= fee_model.commission < Decimal(1):
        raise ValueError("commission must be in [0, 1).")
    if not Decimal(0) <= fee_model.slippage < Decimal(1):
        raise ValueError("slippage must be in [0, 1).")


def _require_matching_executions(
    position: PlannedPosition, entry: EntryExecution, exit: ExitExecution
) -> None:
    if (
        position.entry.candidate != entry.candidate
        or position.entry.execution_asset != entry.execution_asset
        or position.entry.execution_session != entry.execution_session
        or position.exit.candidate != exit.candidate
        or position.exit.execution_asset != exit.execution_asset
        or position.exit.execution_session != exit.execution_session
    ):
        raise ValueError("Priced executions must match the admitted position schedules.")
