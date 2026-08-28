"""Engine-facing Market Data port bound to one immutable DatasetSnapshot."""

from datetime import date
from typing import Protocol

from refinvest.marketdata.domain import (
    CompletedAssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)


class MissingRequiredMarketDataError(ValueError):
    """An expected session has no data in the selected immutable snapshot."""

    error_code = "MISSING_REQUIRED_DATA"


class MarketDataPort(Protocol):
    """Reads only the DatasetSnapshot selected before Backtest execution starts."""

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup: ...


class SessionCalendarPort(Protocol):
    """Immutable expected-session sequence for one pinned DatasetSnapshot."""

    def expected_sessions(self, asset: str) -> tuple[date, ...]: ...

    def expected_session_completions(self, asset: str) -> tuple[CompletedAssetSession, ...]: ...


def require_daily_bar(lookup: DailyBarLookup) -> DailyBar:
    """Return a bar only when present; never fabricate or fall back to another session."""
    if lookup.availability is DailyBarAvailability.AVAILABLE:
        assert lookup.bar is not None
        return lookup.bar
    if lookup.availability is DailyBarAvailability.MISSING_EXPECTED_SESSION:
        raise MissingRequiredMarketDataError(
            "The selected DatasetSnapshot lacks an expected Daily Bar."
        )
    raise ValueError("A non-trading session cannot be used where a Daily Bar is required.")
