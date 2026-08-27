"""In-memory immutable snapshot adapter used until snapshot object storage is introduced."""

from collections.abc import Iterable
from datetime import date

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.domain import (
    AssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)


class InMemorySnapshotDailyBars:
    """One immutable set of canonical bars and calendar-classified non-sessions."""

    def __init__(
        self,
        snapshot_id: str,
        bars: Iterable[DailyBar],
        non_trading_sessions: Iterable[AssetSession] = (),
    ) -> None:
        self.snapshot_id = snapshot_id
        self._bars: dict[AssetSession, DailyBar] = {}
        for bar in bars:
            if bar.asset_session in self._bars:
                raise ValueError(
                    "A DatasetSnapshot cannot contain duplicate AssetSession Daily Bars."
                )
            self._bars[bar.asset_session] = bar
        self._non_trading_sessions = frozenset(non_trading_sessions)
        if self._non_trading_sessions.intersection(self._bars):
            raise ValueError(
                "An AssetSession cannot be both a Daily Bar and a non-trading session."
            )

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        asset_session = AssetSession(asset, session_date)
        bar = self._bars.get(asset_session)
        if bar is not None:
            return DailyBarLookup(DailyBarAvailability.AVAILABLE, bar)
        if asset_session in self._non_trading_sessions:
            return DailyBarLookup(DailyBarAvailability.NON_TRADING_SESSION)
        return DailyBarLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)


class SnapshotMarketData:
    """MarketDataPort adapter fixed to exactly one resolved DatasetSnapshot."""

    def __init__(self, snapshot: DatasetSnapshot, daily_bars: InMemorySnapshotDailyBars) -> None:
        if snapshot.snapshot_id != daily_bars.snapshot_id:
            raise ValueError("Daily Bars must belong to the resolved DatasetSnapshot.")
        self.snapshot = snapshot
        self._daily_bars = daily_bars

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        return self._daily_bars.get_daily_bar(asset, session_date)
