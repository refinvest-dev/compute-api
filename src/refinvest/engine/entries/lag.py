"""Resolve signal lag only on the explicit Primary Signal Asset calendar."""

from dataclasses import dataclass
from datetime import date

from refinvest.engine.signals import Signal
from refinvest.engine.temporal import resolve_lagged_signal_session
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.ports import SessionCalendarPort


@dataclass(frozen=True)
class EntryCandidate:
    """A signal with its ADR-020 lagged Primary Signal Asset session."""

    primary_signal_asset: AssetSymbol
    signal_session: date
    entry_signal_session: date


def resolve_entry_candidates(
    calendar: SessionCalendarPort,
    primary_signal_asset: AssetSymbol,
    lag: int,
    signals: tuple[Signal, ...],
) -> tuple[EntryCandidate, ...]:
    """Apply a non-negative lag to ordered signals without resolving execution sessions."""
    if lag < 0:
        raise ValueError("lag must not be negative.")
    _require_matching_ordered_signals(primary_signal_asset, signals)
    return tuple(
        EntryCandidate(
            primary_signal_asset,
            signal.session_date,
            resolve_lagged_signal_session(
                calendar, primary_signal_asset.value, signal.session_date, lag
            ),
        )
        for signal in signals
    )


def _require_matching_ordered_signals(
    primary_signal_asset: AssetSymbol, signals: tuple[Signal, ...]
) -> None:
    if any(signal.primary_signal_asset is not primary_signal_asset for signal in signals):
        raise ValueError("All signals must use the explicit Primary Signal Asset.")
    sessions = tuple(signal.session_date for signal in signals)
    if any(previous >= current for previous, current in zip(sessions, sessions[1:], strict=False)):
        raise ValueError("Signals must be strictly ascending and unique.")
