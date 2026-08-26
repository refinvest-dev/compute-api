"""Immutable DatasetSnapshot selection and Backtest job pinning."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class DatasetSnapshot:
    snapshot_id: str
    created_at: datetime
    source: str
    coverage: dict[str, object]
    adjustment_policy: str
    storage_path: str


class DatasetSnapshotRepository(Protocol):
    def get(self, snapshot_id: str) -> DatasetSnapshot | None: ...

    def latest(self) -> DatasetSnapshot | None: ...


class DatasetSnapshotPinner(Protocol):
    def pin_dataset_snapshot(self, run_id: str, snapshot_id: str) -> bool: ...


class DatasetSnapshotResolutionError(ValueError):
    """A requested or latest immutable snapshot could not be resolved."""


def resolve_and_pin_dataset_snapshot(
    run_id: str,
    requested_snapshot_id: str | None,
    snapshots: DatasetSnapshotRepository,
    jobs: DatasetSnapshotPinner,
) -> DatasetSnapshot:
    """Resolve one immutable snapshot and atomically pin it to a pending job."""
    snapshot = snapshots.get(requested_snapshot_id) if requested_snapshot_id else snapshots.latest()
    if snapshot is None:
        raise DatasetSnapshotResolutionError("No matching DatasetSnapshot is available.")
    if not jobs.pin_dataset_snapshot(run_id, snapshot.snapshot_id):
        raise DatasetSnapshotResolutionError(
            "DatasetSnapshot was already pinned or the job is not pending."
        )
    return snapshot
