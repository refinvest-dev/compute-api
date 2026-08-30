"""In-memory immutable snapshot adapter used until snapshot object storage is introduced."""

from collections.abc import Iterable, Mapping
from datetime import date

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    ClosePriceLookup,
    CompletedAssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
    ReferenceClose,
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
        sessions: Mapping[AssetSession, CalendarSession],
    ) -> None:
        self.snapshot_id = snapshot_id
        self.provenance = provenance
        self._sessions = dict(sessions)
        for asset_session, session in self._sessions.items():
            if (
                calendar_for_asset(asset_session.asset) is AssetCalendar.CRYPTO_UTC
                and session.state is not CalendarSessionState.EXPECTED
            ):
                raise ValueError("CRYPTO_UTC sessions must always be expected.")
        self._validate_completion_ordering()

    def session_state(self, asset_session: AssetSession) -> CalendarSessionState:
        return self._session(asset_session).state

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return tuple(
            completed.asset_session.session_date
            for completed in self.expected_session_completions(asset)
        )

    def expected_session_completions(self, asset: str) -> tuple[CompletedAssetSession, ...]:
        expected_sessions: list[CompletedAssetSession] = []
        for asset_session, session in self._sessions.items():
            if asset_session.asset != asset or session.state is not CalendarSessionState.EXPECTED:
                continue
            assert session.completed_at is not None
            expected_sessions.append(CompletedAssetSession(asset_session, session.completed_at))
        return tuple(
            sorted(expected_sessions, key=lambda session: session.asset_session.session_date)
        )

    def _session(self, asset_session: AssetSession) -> CalendarSession:
        try:
            return self._sessions[asset_session]
        except KeyError as error:
            raise CalendarResolutionError(
                "The selected DatasetSnapshot lacks an AssetSession calendar classification."
            ) from error

    def _validate_completion_ordering(self) -> None:
        assets = {asset_session.asset for asset_session in self._sessions}
        for asset in assets:
            completions = self.expected_session_completions(asset)
            if any(
                later.completed_at <= earlier.completed_at
                for earlier, later in zip(completions, completions[1:], strict=False)
            ):
                raise ValueError("Expected AssetSession completion times must be strictly ordered.")


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


class InMemorySnapshotReferenceCloses:
    """Immutable close-only values for non-executable snapshot reference assets."""

    def __init__(self, snapshot_id: str, closes: Iterable[ReferenceClose]) -> None:
        self.snapshot_id = snapshot_id
        self._closes: dict[AssetSession, ReferenceClose] = {}
        for reference_close in closes:
            if reference_close.asset_session in self._closes:
                raise ValueError(
                    "A DatasetSnapshot cannot contain duplicate AssetSession reference closes."
                )
            self._closes[reference_close.asset_session] = reference_close

    @property
    def asset_sessions(self) -> frozenset[AssetSession]:
        return frozenset(self._closes)

    def get(self, asset_session: AssetSession) -> ReferenceClose | None:
        return self._closes.get(asset_session)


class SnapshotMarketData:
    """MarketDataPort adapter fixed to exactly one resolved DatasetSnapshot."""

    def __init__(
        self,
        snapshot: DatasetSnapshot,
        calendar: InMemorySnapshotCalendar,
        daily_bars: InMemorySnapshotDailyBars,
        reference_closes: InMemorySnapshotReferenceCloses | None = None,
    ) -> None:
        if (
            snapshot.snapshot_id != calendar.snapshot_id
            or snapshot.snapshot_id != daily_bars.snapshot_id
            or (
                reference_closes is not None
                and snapshot.snapshot_id != reference_closes.snapshot_id
            )
        ):
            raise ValueError("Calendar and Daily Bars must belong to the resolved DatasetSnapshot.")
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
            if asset_session.asset == "VIX":
                raise ValueError("VIX must use a close-only reference record, not a Daily Bar.")
            if calendar.session_state(asset_session) is not CalendarSessionState.EXPECTED:
                raise ValueError("A Daily Bar can only exist for an expected AssetSession.")
        if reference_closes is not None:
            for asset_session in reference_closes.asset_sessions:
                if calendar.session_state(asset_session) is not CalendarSessionState.EXPECTED:
                    raise ValueError(
                        "A reference close can only exist for an expected AssetSession."
                    )
                if asset_session in daily_bars.asset_sessions:
                    raise ValueError(
                        "An AssetSession cannot contain both a Daily Bar and a reference close."
                    )
        self.snapshot = snapshot
        self._calendar = calendar
        self._daily_bars = daily_bars
        self._reference_closes = reference_closes or InMemorySnapshotReferenceCloses(
            snapshot.snapshot_id, ()
        )

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        asset_session = AssetSession(asset, session_date)
        state = self._calendar.session_state(asset_session)
        if state is CalendarSessionState.NON_TRADING:
            return DailyBarLookup(DailyBarAvailability.NON_TRADING_SESSION)
        bar = self._daily_bars.get(asset_session)
        if bar is None:
            return DailyBarLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)
        return DailyBarLookup(DailyBarAvailability.AVAILABLE, bar)

    def get_close(self, asset: str, session_date: date) -> ClosePriceLookup:
        asset_session = AssetSession(asset, session_date)
        state = self._calendar.session_state(asset_session)
        if state is CalendarSessionState.NON_TRADING:
            return ClosePriceLookup(DailyBarAvailability.NON_TRADING_SESSION)
        bar = self._daily_bars.get(asset_session)
        if bar is not None:
            return ClosePriceLookup(DailyBarAvailability.AVAILABLE, bar.close)
        reference_close = self._reference_closes.get(asset_session)
        if reference_close is not None:
            return ClosePriceLookup(DailyBarAvailability.AVAILABLE, reference_close.close)
        return ClosePriceLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)
