from datetime import UTC, datetime

import pytest

from refinvest.application.dataset_snapshots import (
    DatasetSnapshot,
    DatasetSnapshotResolutionError,
    resolve_and_pin_dataset_snapshot,
)


class FakeSnapshots:
    def __init__(self, snapshots: list[DatasetSnapshot]) -> None:
        self.snapshots = snapshots

    def get(self, snapshot_id: str) -> DatasetSnapshot | None:
        return next((item for item in self.snapshots if item.snapshot_id == snapshot_id), None)

    def latest(self) -> DatasetSnapshot | None:
        return max(
            self.snapshots, key=lambda item: (item.created_at, item.snapshot_id), default=None
        )


class FakePinner:
    def __init__(self) -> None:
        self.pinned: dict[str, str] = {}

    def pin_dataset_snapshot(self, run_id: str, snapshot_id: str) -> bool:
        if run_id in self.pinned:
            return False
        self.pinned[run_id] = snapshot_id
        return True


def snapshot(snapshot_id: str, day: int) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id,
        datetime(2025, 1, day, tzinfo=UTC),
        "vendor",
        {"QQQ": {"firstDate": "2020-01-01", "lastDate": "2025-01-01"}},
        "adjusted",
        f"snapshots/{snapshot_id}",
    )


def test_explicit_snapshot_is_selected_and_pinned() -> None:
    pinner = FakePinner()
    resolved = resolve_and_pin_dataset_snapshot(
        "run-1", "old", FakeSnapshots([snapshot("old", 1), snapshot("new", 2)]), pinner
    )
    assert resolved.snapshot_id == "old"
    assert pinner.pinned == {"run-1": "old"}


def test_latest_snapshot_is_selected_once_and_pinned() -> None:
    pinner = FakePinner()
    resolved = resolve_and_pin_dataset_snapshot(
        "run-1", None, FakeSnapshots([snapshot("old", 1), snapshot("new", 2)]), pinner
    )
    assert resolved.snapshot_id == "new"
    with pytest.raises(DatasetSnapshotResolutionError):
        resolve_and_pin_dataset_snapshot(
            "run-1", None, FakeSnapshots([snapshot("newer", 3)]), pinner
        )


def test_missing_snapshot_fails_fast() -> None:
    with pytest.raises(DatasetSnapshotResolutionError):
        resolve_and_pin_dataset_snapshot("run-1", "missing", FakeSnapshots([]), FakePinner())
