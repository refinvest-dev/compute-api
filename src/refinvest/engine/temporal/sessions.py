"""Temporal resolution over immutable DatasetSnapshot calendar artifacts."""

from datetime import date

from refinvest.marketdata.domain import CompletedAssetSession
from refinvest.marketdata.ports import SessionCalendarPort


class TemporalResolutionError(ValueError):
    """A required session cannot be resolved from the selected calendar artifact."""

    error_code = "CALENDAR_RESOLUTION_FAILED"


def resolve_lagged_signal_session(
    calendar: SessionCalendarPort, primary_signal_asset: str, signal_session: date, lag: int
) -> date:
    """Move forward by lag Primary Signal Asset sessions; zero keeps the signal session."""
    if lag < 0:
        raise ValueError("lag must not be negative.")
    return _session_at_offset(calendar, primary_signal_asset, signal_session, lag)


def resolve_holding_exit_signal_session(
    calendar: SessionCalendarPort,
    primary_signal_asset: str,
    entry_signal_session: date,
    holding_signal_sessions: int,
) -> date:
    """Move forward by the configured positive holding period on the signal calendar."""
    if holding_signal_sessions < 1:
        raise ValueError("holding_signal_sessions must be positive.")
    return _session_at_offset(
        calendar, primary_signal_asset, entry_signal_session, holding_signal_sessions
    )


def resolve_next_execution_session(
    calendar: SessionCalendarPort, execution_asset: str, anchor_session: date
) -> date:
    """Return the first Execution Asset session strictly after the signal-derived anchor."""
    for session in _expected_sessions(calendar, execution_asset):
        if session > anchor_session:
            return session
    raise TemporalResolutionError("No later Execution Asset session is available.")


def resolve_metric_anchor_session(
    calendar: SessionCalendarPort,
    primary_signal_asset: str,
    signal_session: date,
    reference_asset: str,
) -> date:
    """Resolve the latest reference session completed at the Primary Signal Timestamp."""
    signal_completion = _completion_for_session(calendar, primary_signal_asset, signal_session)
    reference_completions = _expected_session_completions(calendar, reference_asset)
    for reference_completion in reversed(reference_completions):
        if reference_completion.completed_at <= signal_completion.completed_at:
            return reference_completion.asset_session.session_date
    raise TemporalResolutionError("No Reference Asset session completed by the Signal Timestamp.")


def resolve_metric_lookback_session(
    calendar: SessionCalendarPort, reference_asset: str, anchor_session: date, window: int
) -> date:
    """Return the exact earlier Reference Asset session for a positive metric window."""
    if window < 1:
        raise ValueError("Metric window must be positive.")
    sessions = _expected_sessions(calendar, reference_asset)
    try:
        anchor_index = sessions.index(anchor_session)
    except ValueError as error:
        raise TemporalResolutionError("The metric anchor is not calendar-expected.") from error
    if anchor_index < window:
        raise TemporalResolutionError("The requested metric lookback session is unavailable.")
    return sessions[anchor_index - window]


def _session_at_offset(
    calendar: SessionCalendarPort, asset: str, anchor_session: date, offset: int
) -> date:
    sessions = _expected_sessions(calendar, asset)
    try:
        anchor_index = sessions.index(anchor_session)
    except ValueError as error:
        raise TemporalResolutionError(
            "The Primary Signal session is not calendar-expected."
        ) from error
    target_index = anchor_index + offset
    if target_index >= len(sessions):
        raise TemporalResolutionError("The requested Primary Signal session is unavailable.")
    return sessions[target_index]


def _expected_sessions(calendar: SessionCalendarPort, asset: str) -> tuple[date, ...]:
    sessions = calendar.expected_sessions(asset)
    if sessions != tuple(sorted(set(sessions))):
        raise TemporalResolutionError("Calendar expected sessions must be ordered and unique.")
    return sessions


def _completion_for_session(
    calendar: SessionCalendarPort, asset: str, session_date: date
) -> CompletedAssetSession:
    for completion in _expected_session_completions(calendar, asset):
        if completion.asset_session.session_date == session_date:
            return completion
    raise TemporalResolutionError("The Primary Signal session is not calendar-expected.")


def _expected_session_completions(
    calendar: SessionCalendarPort, asset: str
) -> tuple[CompletedAssetSession, ...]:
    completions = calendar.expected_session_completions(asset)
    session_dates = tuple(item.asset_session.session_date for item in completions)
    if any(item.asset_session.asset != asset for item in completions):
        raise TemporalResolutionError(
            "Calendar completion asset does not match the requested asset."
        )
    if session_dates != tuple(sorted(set(session_dates))):
        raise TemporalResolutionError("Calendar completion sessions must be ordered and unique.")
    if any(
        later.completed_at <= earlier.completed_at
        for earlier, later in zip(completions, completions[1:], strict=False)
    ):
        raise TemporalResolutionError("Calendar completion times must be strictly ordered.")
    return completions
