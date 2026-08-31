"""Publish immutable snapshot artifacts before exposing their metadata to workers."""

from collections.abc import Iterable, Mapping
from typing import Protocol

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    DailyBar,
    ReferenceClose,
)


class SnapshotArtifactPublisher(Protocol):
    def publish(
        self,
        snapshot: DatasetSnapshot,
        provenance: CalendarProvenance,
        sessions: Mapping[AssetSession, CalendarSession],
        daily_bars: Iterable[DailyBar],
        reference_closes: Iterable[ReferenceClose] = (),
        *,
        corporate_actions_applied: bool,
        point_in_time_validation_passed: bool,
    ) -> PublishedSnapshotArtifacts: ...


class DatasetSnapshotMetadataWriter(Protocol):
    def create(self, snapshot: DatasetSnapshot) -> None: ...


def publish_dataset_snapshot(
    snapshot: DatasetSnapshot,
    provenance: CalendarProvenance,
    sessions: Mapping[AssetSession, CalendarSession],
    daily_bars: Iterable[DailyBar],
    reference_closes: Iterable[ReferenceClose] = (),
    *,
    corporate_actions_applied: bool,
    point_in_time_validation_passed: bool,
    artifacts: SnapshotArtifactPublisher,
    metadata: DatasetSnapshotMetadataWriter,
) -> PublishedSnapshotArtifacts:
    """Publish complete artifacts first; only then make the snapshot claimable in metadata."""
    published = artifacts.publish(
        snapshot,
        provenance,
        sessions,
        daily_bars,
        reference_closes,
        corporate_actions_applied=corporate_actions_applied,
        point_in_time_validation_passed=point_in_time_validation_passed,
    )
    metadata.create(snapshot)
    return published
