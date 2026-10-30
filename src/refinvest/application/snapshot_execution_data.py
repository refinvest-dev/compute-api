"""Application boundary for loading one pinned snapshot's execution inputs."""

from dataclasses import dataclass
from typing import Protocol

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.ports import MarketDataPort, SessionCalendarPort


class SnapshotArtifactError(ValueError):
    """A pinned snapshot artifact is unavailable, malformed, or fails integrity checks."""


@dataclass(frozen=True)
class SnapshotExecutionData:
    """Immutable Engine inputs plus ingestion-provided integrity facts."""

    snapshot_id: str
    calendar: SessionCalendarPort
    market_data: MarketDataPort
    corporate_actions_applied: bool
    point_in_time_validation_passed: bool


class SnapshotExecutionDataLoader(Protocol):
    """Load pinned artifacts; raise SnapshotArtifactError on artifact/integrity failure."""

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData: ...
