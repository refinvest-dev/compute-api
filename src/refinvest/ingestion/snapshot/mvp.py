"""Assemble and publish one complete immutable MVP market-data snapshot."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.calendars.crypto_utc import CryptoUtcCalendarArtifact
from refinvest.ingestion.calendars.us_equity import UsEquityCalendarArtifact
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.ingestion.snapshot.calendar import (
    CalendarComponent,
    composite_calendar_provenance,
)
from refinvest.ingestion.snapshot.workflow import (
    DatasetSnapshotMetadataWriter,
    SnapshotArtifactPublisher,
    publish_dataset_snapshot,
)
from refinvest.ingestion.vendors.binance_btcusdt import BtcusdtArchiveBatch
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarSession,
    DailyBar,
)


class EtfDailyBarIngestor(Protocol):
    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]: ...


class BtcusdtDailyBarIngestor(Protocol):
    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> BtcusdtArchiveBatch: ...


@dataclass(frozen=True)
class MvpSnapshotRequest:
    """Caller-supplied metadata for a new immutable MVP DatasetSnapshot."""

    snapshot_id: str
    created_at: datetime
    source: str
    coverage: dict[str, object]
    adjustment_policy: str
    storage_path: str
    corporate_actions_applied: bool
    point_in_time_validation_passed: bool


class MvpSnapshotIngestionCoordinator:
    """Combine approved MVP sources only after their complete calendar coverage validates."""

    def __init__(
        self,
        us_equity_calendar: Callable[
            [date, date, Mapping[date, datetime | None]], UsEquityCalendarArtifact
        ],
        crypto_utc_calendar: Callable[[date, date], CryptoUtcCalendarArtifact],
        etf_bars: EtfDailyBarIngestor,
        btcusdt_bars: BtcusdtDailyBarIngestor,
        artifacts: SnapshotArtifactPublisher,
        metadata: DatasetSnapshotMetadataWriter,
    ) -> None:
        self._us_equity_calendar = us_equity_calendar
        self._crypto_utc_calendar = crypto_utc_calendar
        self._etf_bars = etf_bars
        self._btcusdt_bars = btcusdt_bars
        self._artifacts = artifacts
        self._metadata = metadata

    def publish(
        self,
        request: MvpSnapshotRequest,
        start: date,
        end: date,
        official_us_equity_overrides: Mapping[date, datetime | None],
    ) -> PublishedSnapshotArtifacts:
        if start > end:
            raise ValueError("Snapshot coverage start must not exceed end.")
        us_equity = self._us_equity_calendar(start, end, official_us_equity_overrides)
        crypto_utc = self._crypto_utc_calendar(start, end)
        sessions = _combined_sessions(us_equity, crypto_utc)
        components = (
            CalendarComponent(
                AssetCalendar.US_EQUITY, _asset_names(us_equity), us_equity.provenance
            ),
            CalendarComponent(
                AssetCalendar.CRYPTO_UTC, _asset_names(crypto_utc), crypto_utc.provenance
            ),
        )
        provenance = composite_calendar_provenance(components, sessions)
        btcusdt = self._btcusdt_bars.fetch_for_calendar(sessions, start, end)
        snapshot = DatasetSnapshot(
            request.snapshot_id,
            request.created_at,
            request.source,
            request.coverage,
            request.adjustment_policy,
            request.storage_path,
            provenance.source,
            provenance.version,
            provenance.content_hash,
            btcusdt.source_artifacts,
        )
        daily_bars = (
            *self._etf_bars.fetch_for_calendar(sessions, start, end),
            *btcusdt.daily_bars,
        )
        return publish_dataset_snapshot(
            snapshot,
            provenance,
            sessions,
            daily_bars,
            calendar_components=components,
            corporate_actions_applied=request.corporate_actions_applied,
            point_in_time_validation_passed=request.point_in_time_validation_passed,
            artifacts=self._artifacts,
            metadata=self._metadata,
        )


def _combined_sessions(
    us_equity: UsEquityCalendarArtifact,
    crypto_utc: CryptoUtcCalendarArtifact,
) -> dict[AssetSession, CalendarSession]:
    sessions = {**us_equity.sessions, **crypto_utc.sessions}
    if len(sessions) != len(us_equity.sessions) + len(crypto_utc.sessions):
        raise ValueError("MVP calendar components must not overlap.")
    return sessions


def _asset_names(
    artifact: UsEquityCalendarArtifact | CryptoUtcCalendarArtifact,
) -> tuple[str, ...]:
    return tuple(sorted({asset_session.asset for asset_session in artifact.sessions}))
