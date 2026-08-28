"""Create deterministic signals from one pinned DatasetSnapshot."""

from dataclasses import dataclass
from datetime import date

from refinvest.engine.conditions import evaluate_condition_group
from refinvest.engine.validation.models import AssetSymbol, StrategyVersion
from refinvest.marketdata.ports import MarketDataPort, SessionCalendarPort


@dataclass(frozen=True)
class Signal:
    """One accepted condition result on the explicit Primary Signal Asset calendar."""

    primary_signal_asset: AssetSymbol
    session_date: date


def evaluate_signals(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    strategy: StrategyVersion,
    signal_sessions: tuple[date, ...],
) -> tuple[Signal, ...]:
    """Return signals for an already resolved, strictly ordered signal-session sequence."""
    _require_strictly_ascending(signal_sessions)
    return tuple(
        Signal(strategy.primary_signal_asset, signal_session)
        for signal_session in signal_sessions
        if evaluate_condition_group(
            calendar,
            market_data,
            strategy.primary_signal_asset,
            signal_session,
            strategy.conditions,
        )
    )


def _require_strictly_ascending(signal_sessions: tuple[date, ...]) -> None:
    if any(
        previous >= current
        for previous, current in zip(signal_sessions, signal_sessions[1:], strict=False)
    ):
        raise ValueError("Signal sessions must be strictly ascending and unique.")
