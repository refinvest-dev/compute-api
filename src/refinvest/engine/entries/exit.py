"""Resolve time-based exit candidates on the Primary Signal Asset calendar."""

from dataclasses import dataclass
from datetime import date

from refinvest.engine.entries.lag import EntryCandidate
from refinvest.engine.temporal import resolve_holding_exit_signal_session
from refinvest.marketdata.ports import SessionCalendarPort


@dataclass(frozen=True)
class ExitCandidate:
    entry_candidate: EntryCandidate
    exit_signal_session: date


def resolve_exit_candidates(
    calendar: SessionCalendarPort,
    holding_signal_sessions: int,
    candidates: tuple[EntryCandidate, ...],
) -> tuple[ExitCandidate, ...]:
    """Resolve time-based exits from each lagged entry signal session."""
    return tuple(
        ExitCandidate(
            candidate,
            resolve_holding_exit_signal_session(
                calendar,
                candidate.primary_signal_asset.value,
                candidate.entry_signal_session,
                holding_signal_sessions,
            ),
        )
        for candidate in candidates
    )
