"""Engine-facing Market Data port bound to one immutable DatasetSnapshot."""

from datetime import date
from typing import Protocol

from refinvest.marketdata.domain import DailyBar, DailyBarAvailability, DailyBarLookup


class MissingRequiredMarketDataError(ValueError):
    """An expected session has no data in the selected immutable snapshot."""


class MarketDataPort(Protocol):
    """Reads only the DatasetSnapshot selected before Backtest execution starts."""

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup: ...


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
