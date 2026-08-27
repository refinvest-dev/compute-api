"""In-memory immutable snapshot adapter used until snapshot object storage is introduced."""

from collections.abc import Iterable, Mapping
from datetime import date

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSessionState,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
    calendar_for_asset,
)


class CalendarResolutionError(ValueError):
    """The immutable snapshot lacks a calendar classification for an AssetSession."""


class InMemorySnapshotCalendar:
    """Immutable calendar artifact produced during ingestion for one DatasetSnapshot."""

    def __init__(
        self,
        snapshot_id: str,
        provenance: CalendarProvenance,
        sessions: Mapping[AssetSession, CalendarSessionState],
    ) -> None:
        self.snapshot_id = snapshot_id
        self.provenance = provenance
        self._sessions = dict(sessions)
        for asset_session, state in self._sessions.items():
            if (
                calendar_for_asset(asset_session.asset) is AssetCalendar.CRYPTO_UTC
                and state is not CalendarSessionState.EXPECTED
            ):
                raise ValueError("CRYPTO_UTC sessions must always be expected.")

    def session_state(self, asset_session: AssetSession) -> CalendarSessionState:
        try:
            return self._sessions[asset_session]
        except KeyError as error:
            raise CalendarResolutionError(
                "The selected DatasetSnapshot lacks an AssetSession calendar classification."
            ) from error

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return tuple(
            sorted(
                asset_session.session_date
                for asset_session, state in self._sessions.items()
                if asset_session.asset == asset and state is CalendarSessionState.EXPECTED
            )
        )


class InMemorySnapshotDailyBars:
    """One immutable set of canonical Daily Bars for one DatasetSnapshot."""

    def __init__(
        self,
        snapshot_id: str,
        bars: Iterable[DailyBar],
    ) -> None:
        self.snapshot_id = snapshot_id
        self._bars: dict[AssetSession, DailyBar] = {}
        for bar in bars:
            if bar.asset_session in self._bars:
                raise ValueError(
                    "A DatasetSnapshot cannot contain duplicate AssetSession Daily Bars."
                )
            self._bars[bar.asset_session] = bar

    @property
    def asset_sessions(self) -> frozenset[AssetSession]:
        return frozenset(self._bars)

    def get(self, asset_session: AssetSession) -> DailyBar | None:
        return self._bars.get(asset_session)


class SnapshotMarketData:
    """MarketDataPort adapter fixed to exactly one resolved DatasetSnapshot."""

    def __init__(
        self,
        snapshot: DatasetSnapshot,
        calendar: InMemorySnapshotCalendar,
        daily_bars: InMemorySnapshotDailyBars,
    ) -> None:
        if (
            snapshot.snapshot_id != calendar.snapshot_id
            or snapshot.snapshot_id != daily_bars.snapshot_id
        ):
            raise ValueError(
                "Calendar and Daily Bars must belong to the resolved DatasetSnapshot."
            )
        if (
            snapshot.calendar_source,
            snapshot.calendar_version,
            snapshot.calendar_hash,
        ) != (
            calendar.provenance.source,
            calendar.provenance.version,
            calendar.provenance.content_hash,
        ):
            raise ValueError(
                "Calendar provenance must match the resolved DatasetSnapshot metadata."
            )
        for asset_session in daily_bars.asset_sessions:
            if calendar.session_state(asset_session) is not CalendarSessionState.EXPECTED:
                raise ValueError("A Daily Bar can only exist for an expected AssetSession.")
        self.snapshot = snapshot
        self._calendar = calendar
        self._daily_bars = daily_bars

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        asset_session = AssetSession(asset, session_date)
        state = self._calendar.session_state(asset_session)
        if state is CalendarSessionState.NON_TRADING:
            return DailyBarLookup(DailyBarAvailability.NON_TRADING_SESSION)
        bar = self._daily_bars.get(asset_session)
        if bar is None:
            return DailyBarLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)
        return DailyBarLookup(DailyBarAvailability.AVAILABLE, bar)
