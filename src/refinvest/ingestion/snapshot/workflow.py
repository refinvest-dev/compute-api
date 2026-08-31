"""Publish immutable snapshot artifacts before exposing their metadata to workers."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.ingestion.snapshot.calendar import CalendarComponent
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
        calendar_components: Sequence[CalendarComponent] = (),
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
    calendar_components: Sequence[CalendarComponent] = (),
    corporate_actions_applied: bool,
    point_in_time_validation_passed: bool,
    artifacts: SnapshotArtifactPublisher,
    metadata: DatasetSnapshotMetadataWriter,
) -> PublishedSnapshotArtifacts:
    """Publish complete artifacts first; only then make the snapshot claimable in metadata."""
    published = (
        artifacts.publish(
            snapshot,
            provenance,
            sessions,
            daily_bars,
            reference_closes,
            calendar_components=calendar_components,
            corporate_actions_applied=corporate_actions_applied,
            point_in_time_validation_passed=point_in_time_validation_passed,
        )
        if calendar_components
        else artifacts.publish(
            snapshot,
            provenance,
            sessions,
            daily_bars,
            reference_closes,
            corporate_actions_applied=corporate_actions_applied,
            point_in_time_validation_passed=point_in_time_validation_passed,
        )
    )
    metadata.create(snapshot)
    return published
