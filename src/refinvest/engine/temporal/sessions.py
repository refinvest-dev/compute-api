"""Primary-signal and execution-session navigation over immutable calendar artifacts."""

from datetime import date

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
