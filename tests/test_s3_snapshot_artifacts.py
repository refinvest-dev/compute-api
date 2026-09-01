from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import SnapshotPublicationError
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)
from refinvest.persistence.object_storage.s3_snapshot_artifacts import (
    S3SnapshotArtifactPublisher,
    S3SnapshotExecutionDataLoader,
)
from refinvest.persistence.object_storage.snapshot_artifacts import SnapshotArtifactError


def test_publishes_manifest_last_and_replays_manifest_verified_s3_snapshot() -> None:
    client = FakeS3Client()
    snapshot = _snapshot()

    published = S3SnapshotArtifactPublisher(client).publish(
        snapshot,
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    loaded = S3SnapshotExecutionDataLoader(client).load(snapshot)

    assert published.storage_path == "s3://refinvest-snapshots/daily/snapshot-1"
    assert client.put_keys == [
        "daily/snapshot-1/calendar.json",
        "daily/snapshot-1/daily-bars.ndjson",
        "daily/snapshot-1/manifest.json",
    ]
    assert loaded.market_data.get_daily_bar("QQQ", date(2025, 1, 2)).bar == _bars()[0]


def test_rejects_existing_s3_object_without_overwrite() -> None:
    client = FakeS3Client()
    snapshot = _snapshot()
    publisher = S3SnapshotArtifactPublisher(client)
    publisher.publish(
        snapshot,
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )

    with pytest.raises(SnapshotPublicationError, match="publication failed"):
        publisher.publish(
            snapshot,
            _provenance(),
            _sessions(),
            _bars(),
            corporate_actions_applied=True,
            point_in_time_validation_passed=True,
        )

    assert client.objects[("refinvest-snapshots", "daily/snapshot-1/manifest.json")]


def test_rejects_s3_hash_mismatch() -> None:
    client = FakeS3Client()
    snapshot = _snapshot()
    S3SnapshotArtifactPublisher(client).publish(
        snapshot,
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    client.objects[("refinvest-snapshots", "daily/snapshot-1/daily-bars.ndjson")] = b"tampered"

    with pytest.raises(SnapshotArtifactError, match="SHA-256"):
        S3SnapshotExecutionDataLoader(client).load(snapshot)


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_keys: list[str] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str) -> object:
        assert IfNoneMatch == "*"
        key = (Bucket, Key)
        if key in self.objects:
            raise RuntimeError("PreconditionFailed")
        self.objects[key] = Body
        self.put_keys.append(Key)
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        "s3://refinvest-snapshots/daily/snapshot-1",
        "test-calendar",
        "v1",
        "calendar-provenance-hash",
    )


def _provenance() -> CalendarProvenance:
    return CalendarProvenance("test-calendar", "v1", "calendar-provenance-hash")


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
            Decimal("102"),
            Decimal("99"),
            Decimal("101"),
            Decimal("1000"),
        ),
    )
