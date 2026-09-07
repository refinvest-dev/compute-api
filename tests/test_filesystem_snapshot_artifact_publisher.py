from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot, SourceArtifact
from refinvest.ingestion.snapshot.artifacts import (
    FilesystemSnapshotArtifactPublisher,
    SnapshotPublicationError,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)


def test_publishes_deterministic_artifacts_that_the_loader_can_replay(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot-1", "snapshot-1")

    published = FilesystemSnapshotArtifactPublisher().publish(
        snapshot,
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    loaded = FilesystemSnapshotExecutionDataLoader().load(snapshot)

    assert published.snapshot_id == snapshot.snapshot_id
    assert published.storage_path == snapshot.storage_path
    assert (Path(snapshot.storage_path) / "manifest.json").exists()
    assert b'"formatVersion":3' in (Path(snapshot.storage_path) / "manifest.json").read_bytes()
    assert b'"volume":"1000"' in (Path(snapshot.storage_path) / "daily-bars.ndjson").read_bytes()
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.calendar.expected_sessions("QQQ") == (date(2025, 1, 2), date(2025, 1, 3))
    assert loaded.market_data.get_daily_bar("QQQ", date(2025, 1, 3)).bar == _bars()[0]


def test_records_verified_source_artifacts_in_immutable_manifest(tmp_path: Path) -> None:
    snapshot = DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "BINANCE_PUBLIC_DATA",
        {},
        "unadjusted",
        str(tmp_path / "snapshot-1"),
        "test-calendar",
        "v1",
        "calendar-provenance-hash",
        (
            SourceArtifact(
                "https://data.binance.vision/archive.zip",
                "a" * 64,
                datetime(2025, 1, 2, tzinfo=UTC),
            ),
        ),
    )

    FilesystemSnapshotArtifactPublisher().publish(
        snapshot,
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=False,
        point_in_time_validation_passed=True,
    )

    manifest = (Path(snapshot.storage_path) / "manifest.json").read_text(encoding="utf-8")
    assert '"uri":"https://data.binance.vision/archive.zip"' in manifest
    assert '"sha256":"' + "a" * 64 + '"' in manifest
    assert '"retrievedAt":"2025-01-02T00:00:00Z"' in manifest


def test_publication_is_byte_deterministic_regardless_of_input_order(tmp_path: Path) -> None:
    first = _snapshot(tmp_path / "first", "snapshot-1")
    second = _snapshot(tmp_path / "second", "snapshot-1")
    publisher = FilesystemSnapshotArtifactPublisher()

    publisher.publish(
        first,
        _provenance(),
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    publisher.publish(
        second,
        _provenance(),
        dict(reversed(tuple(_sessions().items()))),
        reversed(_bars()),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )

    for name in ("calendar.json", "daily-bars.ndjson", "manifest.json"):
        assert (Path(first.storage_path) / name).read_bytes() == (
            Path(second.storage_path) / name
        ).read_bytes()


def test_rejects_existing_storage_path_without_overwrite(tmp_path: Path) -> None:
    storage_path = tmp_path / "snapshot-1"
    storage_path.mkdir()
    marker = storage_path / "existing.txt"
    marker.write_text("must remain", encoding="utf-8")

    with pytest.raises(SnapshotPublicationError, match="already exists"):
        FilesystemSnapshotArtifactPublisher().publish(
            _snapshot(storage_path, "snapshot-1"),
            _provenance(),
            _sessions(),
            _bars(),
            corporate_actions_applied=True,
            point_in_time_validation_passed=True,
        )

    assert marker.read_text(encoding="utf-8") == "must remain"


def test_rejects_snapshot_calendar_provenance_mismatch_before_writing(tmp_path: Path) -> None:
    snapshot = DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        str(tmp_path / "snapshot-1"),
        "test-calendar",
        "v2",
        "calendar-provenance-hash",
    )

    with pytest.raises(SnapshotPublicationError, match="provenance"):
        FilesystemSnapshotArtifactPublisher().publish(
            snapshot,
            _provenance(),
            _sessions(),
            _bars(),
            corporate_actions_applied=True,
            point_in_time_validation_passed=True,
        )

    assert not Path(snapshot.storage_path).exists()


def _snapshot(storage_path: Path, snapshot_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id,
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        str(storage_path),
        "test-calendar",
        "v1",
        "calendar-provenance-hash",
    )


def _provenance() -> CalendarProvenance:
    return CalendarProvenance("test-calendar", "v1", "calendar-provenance-hash")


def _sessions() -> dict[AssetSession, CalendarSession]:
    return {
        AssetSession("QQQ", date(2025, 1, 3)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 3, 21, tzinfo=UTC)
        ),
        AssetSession("QQQ", date(2025, 1, 2)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
        ),
    }


def _bars() -> tuple[DailyBar, ...]:
    return (
        DailyBar(
            "QQQ",
            date(2025, 1, 3),
            Decimal("101"),
            Decimal("101"),
            Decimal("101"),
            Decimal("101"),
            1000,
        ),
        DailyBar(
            "QQQ",
            date(2025, 1, 2),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            1000,
        ),
    )
