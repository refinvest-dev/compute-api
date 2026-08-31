import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.domain import DailyBarAvailability
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
    SnapshotArtifactError,
)


def test_loads_manifest_verified_immutable_snapshot_artifacts(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path)

    data = FilesystemSnapshotExecutionDataLoader().load(snapshot)

    assert data.snapshot_id == snapshot.snapshot_id
    assert data.corporate_actions_applied
    assert data.point_in_time_validation_passed
    assert data.calendar.expected_sessions("QQQ") == (datetime(2025, 1, 2).date(),)
    lookup = data.market_data.get_daily_bar("QQQ", datetime(2025, 1, 2).date())
    assert lookup.availability is DailyBarAvailability.AVAILABLE
    assert lookup.bar is not None
    assert lookup.bar.close == 101


def test_rejects_artifact_hash_mismatch(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path)
    (tmp_path / "daily-bars.ndjson").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SnapshotArtifactError, match="SHA-256"):
        FilesystemSnapshotExecutionDataLoader().load(snapshot)


def test_preserves_expected_session_gap_for_engine_fail_fast(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path, daily_bars="")

    data = FilesystemSnapshotExecutionDataLoader().load(snapshot)

    lookup = data.market_data.get_daily_bar("QQQ", datetime(2025, 1, 2).date())
    assert lookup.availability is DailyBarAvailability.MISSING_EXPECTED_SESSION


def test_loads_v2_decimal_daily_bar_volume_without_precision_loss(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path, format_version=2, volume='"0.125"')

    data = FilesystemSnapshotExecutionDataLoader().load(snapshot)

    lookup = data.market_data.get_daily_bar("QQQ", datetime(2025, 1, 2).date())
    assert lookup.bar is not None
    assert lookup.bar.volume == Decimal("0.125")


def _write_snapshot(
    tmp_path: Path,
    daily_bars: str | None = None,
    *,
    format_version: int = 1,
    volume: str = "1000",
) -> DatasetSnapshot:
    calendar = {
        "provenance": {
            "source": "test-calendar",
            "version": "v1",
            "contentHash": "calendar-provenance-hash",
        },
        "sessions": [
            {
                "asset": "QQQ",
                "sessionDate": "2025-01-02",
                "state": "EXPECTED",
                "completedAt": "2025-01-02T21:00:00Z",
            }
        ],
    }
    calendar_content = json.dumps(calendar, separators=(",", ":"), sort_keys=True).encode()
    bars_content = (
        daily_bars.encode()
        if daily_bars is not None
        else (
            b'{"asset":"QQQ","sessionDate":"2025-01-02","open":"100",'
            b'"high":"102","low":"99","close":"101","volume":' + volume.encode() + b"}\n"
        )
    )
    (tmp_path / "calendar.json").write_bytes(calendar_content)
    (tmp_path / "daily-bars.ndjson").write_bytes(bars_content)
    manifest = {
        "formatVersion": format_version,
        "snapshotId": "snapshot-1",
        "corporateActionsApplied": True,
        "pointInTimeValidationPassed": True,
        "calendar": {"path": "calendar.json", "sha256": _hash(calendar_content)},
        "dailyBars": {"path": "daily-bars.ndjson", "sha256": _hash(bars_content)},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        str(tmp_path),
        "test-calendar",
        "v1",
        "calendar-provenance-hash",
    )


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
