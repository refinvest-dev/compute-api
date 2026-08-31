from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.ingestion.snapshot.workflow import publish_dataset_snapshot
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
    ReferenceClose,
)


class FakeArtifacts:
    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    def publish(
        self,
        snapshot: DatasetSnapshot,
        provenance: CalendarProvenance,
        sessions: dict[AssetSession, CalendarSession],
        daily_bars: tuple[DailyBar, ...],
        reference_closes: tuple[ReferenceClose, ...] = (),
        *,
        corporate_actions_applied: bool,
        point_in_time_validation_passed: bool,
    ) -> PublishedSnapshotArtifacts:
        del (
            provenance,
            sessions,
            daily_bars,
            reference_closes,
            corporate_actions_applied,
            point_in_time_validation_passed,
        )
        self.events.append("publish")
        if self.failure is not None:
            raise self.failure
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


def test_publishes_artifacts_before_inserting_snapshot_metadata() -> None:
    events: list[str] = []
    artifacts = FakeArtifacts(events)
    metadata = FakeMetadata(events)

    result = publish_dataset_snapshot(
        _snapshot(),
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
        artifacts=artifacts,
        metadata=metadata,
    )

    assert events == ["publish", "metadata"]
    assert metadata.snapshots == [_snapshot()]
    assert result.snapshot_id == "snapshot-1"


def test_does_not_expose_metadata_when_artifact_publication_fails() -> None:
    events: list[str] = []
    metadata = FakeMetadata(events)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        publish_dataset_snapshot(
            _snapshot(),
            _provenance(),
            _sessions(),
            _bars(),
            corporate_actions_applied=True,
            point_in_time_validation_passed=True,
            artifacts=FakeArtifacts(events, RuntimeError("storage unavailable")),
            metadata=metadata,
        )

    assert events == ["publish"]
    assert metadata.snapshots == []


def test_publishes_vix_reference_closes_before_inserting_snapshot_metadata() -> None:
    events: list[str] = []
    artifacts = FakeArtifacts(events)
    metadata = FakeMetadata(events)

    publish_dataset_snapshot(
        _snapshot(),
        _provenance(),
        {
            AssetSession("VIX", date(2025, 1, 2)): CalendarSession(
                CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
            )
        },
        (),
        (ReferenceClose("VIX", date(2025, 1, 2), Decimal("18.5")),),
        corporate_actions_applied=False,
        point_in_time_validation_passed=True,
        artifacts=artifacts,
        metadata=metadata,
    )

    assert events == ["publish", "metadata"]


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        "snapshot-1-path",
        "calendar",
        "v1",
        "hash",
    )


def _provenance() -> CalendarProvenance:
    return CalendarProvenance("calendar", "v1", "hash")


def _sessions() -> dict[AssetSession, CalendarSession]:
    return {
        AssetSession("QQQ", date(2025, 1, 2)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
        )
    }


def _bars() -> tuple[DailyBar, ...]:
    return (
        DailyBar(
            "QQQ",
            date(2025, 1, 2),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            1,
        ),
    )
