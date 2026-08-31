from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import (
    FilesystemSnapshotArtifactPublisher,
    SnapshotPublicationError,
)
from refinvest.ingestion.snapshot.calendar import (
    CalendarComponent,
    composite_calendar_provenance,
)
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)


def test_composite_provenance_is_independent_of_component_and_session_input_order() -> None:
    first = composite_calendar_provenance(_components(), _sessions())
    second = composite_calendar_provenance(
        tuple(reversed(_components())), dict(reversed(tuple(_sessions().items())))
    )
    assert first == second


def test_publishes_and_replays_validated_composite_calendar(tmp_path: Path) -> None:
    provenance = composite_calendar_provenance(_components(), _sessions())
    snapshot = _snapshot(tmp_path / "snapshot-1", provenance)

    FilesystemSnapshotArtifactPublisher().publish(
        snapshot,
        provenance,
        _sessions(),
        _bars(),
        calendar_components=tuple(reversed(_components())),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )

    calendar = (Path(snapshot.storage_path) / "calendar.json").read_bytes()
    loaded = FilesystemSnapshotExecutionDataLoader().load(snapshot)

    assert b'"components"' in calendar
    assert b'"calendar":"CRYPTO_UTC"' in calendar
    assert loaded.calendar.expected_sessions("QQQ") == (date(2025, 1, 2),)
    assert loaded.calendar.expected_sessions("BTCUSDT") == (date(2025, 1, 2),)


def test_rejects_composite_component_with_an_asset_from_another_calendar(tmp_path: Path) -> None:
    invalid_components = (
        CalendarComponent(
            AssetCalendar.US_EQUITY,
            ("QQQ", "BTCUSDT"),
            CalendarProvenance("xnys", "1", "hash"),
        ),
        _components()[1],
    )
    provenance = CalendarProvenance("composite-calendar", "1", "not-a-real-hash")

    with pytest.raises(SnapshotPublicationError, match="canonical invariants"):
        FilesystemSnapshotArtifactPublisher().publish(
            _snapshot(tmp_path / "snapshot-1", provenance),
            provenance,
            _sessions(),
            _bars(),
            calendar_components=invalid_components,
            corporate_actions_applied=True,
            point_in_time_validation_passed=True,
        )


def _components() -> tuple[CalendarComponent, ...]:
    return (
        CalendarComponent(
            AssetCalendar.US_EQUITY,
            ("QQQ",),
            CalendarProvenance("xnys", "4.13.2", "xnys-overrides-hash"),
        ),
        CalendarComponent(
            AssetCalendar.CRYPTO_UTC,
            ("BTCUSDT",),
            CalendarProvenance("fixed:CRYPTO_UTC", "1", "crypto-rule-hash"),
        ),
    )


def _sessions() -> dict[AssetSession, CalendarSession]:
    return {
        AssetSession("QQQ", date(2025, 1, 2)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
        ),
        AssetSession("BTCUSDT", date(2025, 1, 2)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 3, tzinfo=UTC)
        ),
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
            Decimal("1"),
        ),
        DailyBar(
            "BTCUSDT",
            date(2025, 1, 2),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0.1"),
        ),
    )


def _snapshot(storage_path: Path, provenance: CalendarProvenance) -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        str(storage_path),
        provenance.source,
        provenance.version,
        provenance.content_hash,
    )
