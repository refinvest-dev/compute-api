"""Admit non-overlapping positions before market-price lookup."""

from dataclasses import dataclass

from refinvest.engine.entries import EntryCandidate, resolve_exit_candidates
from refinvest.engine.execution.schedule import (
    EntryExecutionSchedule,
    ExitExecutionSchedule,
    resolve_entry_execution_schedule,
    resolve_exit_execution_schedule,
)
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.ports import SessionCalendarPort


@dataclass(frozen=True)
class PlannedPosition:
    entry: EntryExecutionSchedule
    exit: ExitExecutionSchedule


def admit_positions(
    calendar: SessionCalendarPort,
    execution_asset: AssetSymbol,
    holding_signal_sessions: int,
    candidates: tuple[EntryCandidate, ...],
) -> tuple[PlannedPosition, ...]:
    """Return non-overlapping positions, ignoring duplicate entry candidates."""
    schedules = sorted(
        (
            resolve_entry_execution_schedule(calendar, execution_asset, candidate)
            for candidate in candidates
        ),
        key=lambda item: (item.execution_session, item.candidate.signal_session),
    )
    accepted: list[PlannedPosition] = []
    active_exit_session = None
    for entry in schedules:
        if active_exit_session is not None and entry.execution_session <= active_exit_session:
            continue
        exit_candidate = resolve_exit_candidates(
            calendar, holding_signal_sessions, (entry.candidate,)
        )[0]
        exit_schedule = resolve_exit_execution_schedule(calendar, execution_asset, exit_candidate)
        accepted.append(PlannedPosition(entry, exit_schedule))
        active_exit_session = exit_schedule.execution_session
    return tuple(accepted)
