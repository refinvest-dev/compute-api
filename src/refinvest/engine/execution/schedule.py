"""Execution-session schedules without market-price reads."""

from dataclasses import dataclass
from datetime import date

from refinvest.engine.entries import EntryCandidate, ExitCandidate
from refinvest.engine.temporal import resolve_next_execution_session
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.ports import SessionCalendarPort


@dataclass(frozen=True)
class EntryExecutionSchedule:
    candidate: EntryCandidate
    execution_asset: AssetSymbol
    execution_session: date


@dataclass(frozen=True)
class ExitExecutionSchedule:
    candidate: ExitCandidate
    execution_asset: AssetSymbol
    execution_session: date


def resolve_entry_execution_schedule(
    calendar: SessionCalendarPort, execution_asset: AssetSymbol, candidate: EntryCandidate
) -> EntryExecutionSchedule:
    return EntryExecutionSchedule(
        candidate,
        execution_asset,
        resolve_next_execution_session(
            calendar, execution_asset.value, candidate.entry_signal_session
        ),
    )


def resolve_exit_execution_schedule(
    calendar: SessionCalendarPort, execution_asset: AssetSymbol, candidate: ExitCandidate
) -> ExitExecutionSchedule:
    return ExitExecutionSchedule(
        candidate,
        execution_asset,
        resolve_next_execution_session(
            calendar, execution_asset.value, candidate.exit_signal_session
        ),
    )
