from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from refinvest.app.config import Settings
from refinvest.app.snapshot_ingestion import run_snapshot_ingestion
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.ingestion.snapshot.mvp import MvpSnapshotRequest


def test_builds_explicit_completed_snapshot_request_and_printable_result(tmp_path: Path) -> None:
    overrides = tmp_path / "overrides.json"
    overrides.write_text('{"2025-01-02":"2025-01-02T18:00:00Z"}', encoding="utf-8")
    coordinator = FakeCoordinator()

    result = run_snapshot_ingestion(
        [
            "--snapshot-id",
            "snapshot-20250102",
            "--start",
            "2025-01-02",
            "--end",
            "2025-01-02",
            "--us-equity-overrides",
            str(overrides),
        ],
        settings=Settings(
            internal_api_key=None,
            database_url="postgresql://example",
            massive_api_key="secret",
            snapshot_storage_root=str(tmp_path / "snapshots"),
        ),
        now=lambda: datetime(2025, 1, 3, tzinfo=UTC),
        coordinator_factory=lambda _: coordinator,  # type: ignore[return-value]
    )

    assert result == (
        '{"snapshotId":"snapshot-20250102","storagePath":"published/snapshot-20250102"}'
    )
    assert coordinator.request == MvpSnapshotRequest(
        "snapshot-20250102",
        datetime(2025, 1, 3, tzinfo=UTC),
        "massive-eod-us-stocks+cboe-vix-history+binance-spot",
        {"firstDate": "2025-01-02", "lastDate": "2025-01-02"},
        "split-adjusted-ohlcv-cash-dividends-unadjusted",
        str(tmp_path / "snapshots" / "snapshot-20250102"),
        True,
        True,
    )
    assert coordinator.overrides == {date(2025, 1, 2): datetime(2025, 1, 2, 18, tzinfo=UTC)}


@pytest.mark.parametrize(
    "arguments, message",
    [
        (
            ["--snapshot-id", "../unsafe", "--start", "2025-01-02", "--end", "2025-01-02"],
            "safe immutable",
        ),
        (
            ["--snapshot-id", "safe", "--start", "2025-01-03", "--end", "2025-01-02"],
            "start must not exceed",
        ),
        (
            ["--snapshot-id", "safe", "--start", "2025-01-03", "--end", "2025-01-03"],
            "must precede",
        ),
    ],
)
def test_rejects_unsafe_or_incomplete_snapshot_coverage(
    tmp_path: Path, arguments: list[str], message: str
) -> None:
    overrides = tmp_path / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")
    settings = Settings(None, snapshot_storage_root=str(tmp_path / "snapshots"))

    with pytest.raises(ValueError, match=message):
        run_snapshot_ingestion(
            [*arguments, "--us-equity-overrides", str(overrides)],
            settings=settings,
            now=lambda: datetime(2025, 1, 3, tzinfo=UTC),
            coordinator_factory=lambda _: FakeCoordinator(),  # type: ignore[return-value]
        )


class FakeCoordinator:
    def __init__(self) -> None:
        self.request: MvpSnapshotRequest | None = None
        self.overrides: dict[date, datetime | None] = {}

    def publish(
        self,
        request: MvpSnapshotRequest,
        start: date,
        end: date,
        overrides: dict[date, datetime | None],
    ) -> PublishedSnapshotArtifacts:
        assert start == end == date(2025, 1, 2)
        self.request = request
        self.overrides = overrides
        return PublishedSnapshotArtifacts(
            request.snapshot_id, f"published/{request.snapshot_id}", "x", "y"
        )
