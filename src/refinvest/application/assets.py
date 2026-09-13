"""Compute-owned static asset registry and snapshot-backed availability."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader


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


class DatasetSnapshotSelector(Protocol):
    def latest(self) -> DatasetSnapshot | None: ...


def asset_reference(symbol: str) -> AssetReference | None:
    return _BY_SYMBOL.get(symbol)


def availability_for_snapshot(
    asset: AssetReference, snapshot: DatasetSnapshot, loader: SnapshotExecutionDataLoader
) -> AssetAvailability | None:
    """Use only the selected immutable snapshot's verified calendar artifact."""
    sessions = loader.load(snapshot).calendar.expected_sessions(asset.symbol)
    if not sessions:
        return None
    return AssetAvailability(
        min(sessions), max(sessions), snapshot.snapshot_id, snapshot.created_at
    )
