"""Compute-owned static asset registry and snapshot-backed availability."""

from dataclasses import dataclass
from datetime import date, datetime

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader
from refinvest.marketdata.ports import SessionCalendarPort


@dataclass(frozen=True)
class AssetReference:
    symbol: str
    calendar: str
    inception_date: date
    execution_enabled: bool


ASSET_REFERENCES = (
    AssetReference("QQQ", "US_EQUITY", date(1999, 3, 10), True),
    AssetReference("SPY", "US_EQUITY", date(1993, 1, 22), True),
    AssetReference("TQQQ", "US_EQUITY", date(2010, 2, 9), True),
    AssetReference("SOXL", "US_EQUITY", date(2010, 3, 11), True),
    AssetReference("BTCUSDT", "CRYPTO_UTC", date(2017, 8, 17), True),
)
_BY_SYMBOL = {asset.symbol: asset for asset in ASSET_REFERENCES}


@dataclass(frozen=True)
class AssetAvailability:
    first_date: date
    last_date: date
    dataset_snapshot_id: str
    snapshot_created_at: datetime


def asset_reference(symbol: str) -> AssetReference | None:
    return _BY_SYMBOL.get(symbol)


def availability_for_snapshot(
    asset: AssetReference, snapshot: DatasetSnapshot, loader: SnapshotExecutionDataLoader
) -> AssetAvailability | None:
    """Use only the selected immutable snapshot's verified calendar artifact."""
    return _availability_for_calendar(asset, snapshot, loader.load(snapshot).calendar)


def availabilities_for_snapshot(
    snapshot: DatasetSnapshot | None, loader: SnapshotExecutionDataLoader
) -> dict[str, AssetAvailability]:
    """Validate once and reuse the selected snapshot's calendar only within this call."""
    if snapshot is None:
        return {}
    calendar = loader.load(snapshot).calendar
    return {
        asset.symbol: availability
        for asset in ASSET_REFERENCES
        if (availability := _availability_for_calendar(asset, snapshot, calendar)) is not None
    }


def _availability_for_calendar(
    asset: AssetReference, snapshot: DatasetSnapshot, calendar: SessionCalendarPort
) -> AssetAvailability | None:
    sessions = calendar.expected_sessions(asset.symbol)
    if not sessions:
        return None
    return AssetAvailability(
        min(sessions), max(sessions), snapshot.snapshot_id, snapshot.created_at
    )
