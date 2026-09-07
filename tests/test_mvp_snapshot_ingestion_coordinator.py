from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.calendars.crypto_utc import CryptoUtcCalendarArtifact
from refinvest.ingestion.calendars.us_equity import US_EQUITY_ASSETS, UsEquityCalendarArtifact
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.ingestion.snapshot.calendar import CalendarComponent
from refinvest.ingestion.snapshot.mvp import (
    MvpSnapshotIngestionCoordinator,
    MvpSnapshotRequest,
)
from refinvest.ingestion.vendors.binance_btcusdt import BtcusdtArchiveBatch
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
    ReferenceClose,
)


def test_publishes_one_complete_mvp_snapshot_after_all_sources_validate() -> None:
    events: list[str] = []
    artifacts = FakeArtifacts(events)
    metadata = FakeMetadata(events)
    coordinator = MvpSnapshotIngestionCoordinator(
        us_calendar,
        crypto_calendar,
        FakeEtfBars(),
        FakeBtcusdtBars(),
        artifacts,
        metadata,
    )

    result = coordinator.publish(_request(), date(2025, 1, 2), date(2025, 1, 2), {})

    assert result.snapshot_id == "snapshot-1"
    assert events == ["publish", "metadata"]
    assert metadata.snapshots[0].calendar_source == "composite-calendar"
    assert artifacts.components is not None
    assert {component.calendar.value for component in artifacts.components} == {
        "US_EQUITY",
        "CRYPTO_UTC",
    }
    assert {bar.asset for bar in artifacts.daily_bars} == {"QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT"}
    assert artifacts.reference_closes == ()


def test_does_not_publish_when_a_vendor_fails() -> None:
    events: list[str] = []
    coordinator = MvpSnapshotIngestionCoordinator(
        us_calendar,
        crypto_calendar,
        FailingEtfBars(),
        FakeBtcusdtBars(),
        FakeArtifacts(events),
        FakeMetadata(events),
    )

    with pytest.raises(RuntimeError, match="Massive unavailable"):
        coordinator.publish(_request(), date(2025, 1, 2), date(2025, 1, 2), {})

    assert events == []


class FakeArtifacts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.components: tuple[CalendarComponent, ...] | None = None
        self.daily_bars: tuple[DailyBar, ...] = ()
        self.reference_closes: tuple[ReferenceClose, ...] = ()

    def publish(
        self,
        snapshot: DatasetSnapshot,
        provenance: CalendarProvenance,
        sessions: Mapping[AssetSession, CalendarSession],
        daily_bars: tuple[DailyBar, ...],
        reference_closes: tuple[ReferenceClose, ...] = (),
        *,
        calendar_components: tuple[CalendarComponent, ...] = (),
        corporate_actions_applied: bool,
        point_in_time_validation_passed: bool,
    ) -> PublishedSnapshotArtifacts:
        del provenance, sessions, corporate_actions_applied, point_in_time_validation_passed
        self.events.append("publish")
        self.components = calendar_components
        self.daily_bars = daily_bars
        self.reference_closes = reference_closes
        return PublishedSnapshotArtifacts(
            snapshot.snapshot_id, snapshot.storage_path, "calendar", "bars"
        )


class FakeMetadata:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.snapshots: list[DatasetSnapshot] = []

    def create(self, snapshot: DatasetSnapshot) -> None:
        self.events.append("metadata")
        self.snapshots.append(snapshot)


class FakeEtfBars:
    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]:
        del sessions, start, end
        return tuple(_bar(asset) for asset in ("QQQ", "SPY", "TQQQ", "SOXL"))


class FailingEtfBars:
    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]:
        del sessions, start, end
        raise RuntimeError("Massive unavailable")


class FakeBtcusdtBars:
    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> BtcusdtArchiveBatch:
        del sessions, start, end
        return BtcusdtArchiveBatch((_bar("BTCUSDT"),), ())


def us_calendar(
    start: date,
    end: date,
    overrides: Mapping[date, datetime | None],
) -> UsEquityCalendarArtifact:
    del overrides
    assert start == end == date(2025, 1, 2)
    return UsEquityCalendarArtifact(
        CalendarProvenance("xnys", "4.13.2", "xnys-hash"),
        {
            AssetSession(asset, start): CalendarSession(
                CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
            )
            for asset in US_EQUITY_ASSETS
        },
    )


def crypto_calendar(start: date, end: date) -> CryptoUtcCalendarArtifact:
    assert start == end == date(2025, 1, 2)
    return CryptoUtcCalendarArtifact(
        CalendarProvenance("fixed:CRYPTO_UTC", "1", "crypto-hash"),
        {
            AssetSession("BTCUSDT", start): CalendarSession(
                CalendarSessionState.EXPECTED, datetime(2025, 1, 3, tzinfo=UTC)
            )
        },
    )


def _bar(asset: str) -> DailyBar:
    return DailyBar(
        asset,
        date(2025, 1, 2),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("1"),
    )


def _request() -> MvpSnapshotRequest:
    return MvpSnapshotRequest(
        "snapshot-1",
        datetime(2025, 1, 3, tzinfo=UTC),
        "mvp-approved-sources",
        {"start": "2025-01-02", "end": "2025-01-02"},
        "split-adjusted-ohlcv-cash-dividends-unadjusted",
        "snapshots/snapshot-1",
        True,
        True,
    )
