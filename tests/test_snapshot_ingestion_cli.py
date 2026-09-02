import json
import logging
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path

import pytest

from refinvest.app.config import Settings
from refinvest.app.snapshot_ingestion import run_snapshot_ingestion
from refinvest.ingestion.snapshot.artifacts import PublishedSnapshotArtifacts
from refinvest.ingestion.snapshot.mvp import MvpSnapshotRequest

snapshot_ingestion = import_module("refinvest.app.snapshot_ingestion")


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
    assert coordinator.start == coordinator.end == date(2025, 1, 2)


def test_uses_lifecycle_defaults_and_generates_a_new_snapshot_id(tmp_path: Path) -> None:
    overrides = tmp_path / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")
    coordinator = FakeCoordinator()

    run_snapshot_ingestion(
        [],
        settings=Settings(
            None,
            snapshot_storage_root=str(tmp_path / "snapshots"),
            snapshot_start_date="2020-01-01",
            us_equity_overrides_path=str(overrides),
        ),
        now=lambda: datetime(2025, 1, 3, 0, 15, tzinfo=UTC),
        snapshot_nonce=lambda: "abc123",
        coordinator_factory=lambda _: coordinator,  # type: ignore[return-value]
    )

    assert coordinator.request is not None
    assert coordinator.request.snapshot_id == "mvp-20250102-20250103T001500Z-abc123"
    assert coordinator.request.coverage == {"firstDate": "2020-01-01", "lastDate": "2025-01-02"}
    assert coordinator.start == date(2020, 1, 1)
    assert coordinator.end == date(2025, 1, 2)


def test_uses_configured_s3_prefix_for_snapshot_identity(tmp_path: Path) -> None:
    overrides = tmp_path / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")
    coordinator = FakeCoordinator()

    run_snapshot_ingestion(
        [],
        settings=Settings(
            None,
            snapshot_storage_backend="s3",
            snapshot_storage_s3_bucket="refinvest-snapshots",
            snapshot_storage_s3_prefix="daily",
            snapshot_start_date="2020-01-01",
            us_equity_overrides_path=str(overrides),
        ),
        now=lambda: datetime(2025, 1, 3, 0, 15, tzinfo=UTC),
        snapshot_nonce=lambda: "abc123",
        coordinator_factory=lambda _: coordinator,  # type: ignore[return-value]
    )

    assert coordinator.request is not None
    assert coordinator.request.storage_path == (
        "s3://refinvest-snapshots/daily/mvp-20250102-20250103T001500Z-abc123"
    )


def test_records_structured_publication_lifecycle_events(tmp_path: Path, caplog) -> None:
    overrides = tmp_path / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="refinvest.app.snapshot_ingestion"):
        run_snapshot_ingestion(
            [],
            settings=Settings(
                None,
                snapshot_storage_root=str(tmp_path / "snapshots"),
                snapshot_start_date="2020-01-01",
                us_equity_overrides_path=str(overrides),
            ),
            now=lambda: datetime(2025, 1, 3, 0, 15, tzinfo=UTC),
            snapshot_nonce=lambda: "abc123",
            coordinator_factory=lambda _: FakeCoordinator(),  # type: ignore[return-value]
        )

    events = [json.loads(record.message) for record in caplog.records]
    assert [event["event"] for event in events] == [
        "snapshot_publication_started",
        "snapshot_publication_completed",
    ]
    assert events[0]["snapshot_id"] == "mvp-20250102-20250103T001500Z-abc123"
    assert events[1]["coverage_start"] == "2020-01-01"
    assert events[1]["coverage_end"] == "2025-01-02"


def test_records_structured_publication_failure_without_error_details(
    tmp_path: Path, caplog
) -> None:
    overrides = tmp_path / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="refinvest.app.snapshot_ingestion"):
        with pytest.raises(RuntimeError, match="vendor failed"):
            run_snapshot_ingestion(
                [],
                settings=Settings(
                    None,
                    snapshot_storage_root=str(tmp_path / "snapshots"),
                    snapshot_start_date="2020-01-01",
                    us_equity_overrides_path=str(overrides),
                ),
                now=lambda: datetime(2025, 1, 3, 0, 15, tzinfo=UTC),
                snapshot_nonce=lambda: "abc123",
                coordinator_factory=lambda _: FailingCoordinator(),  # type: ignore[return-value]
            )

    events = [json.loads(record.message) for record in caplog.records]
    assert [event["event"] for event in events] == [
        "snapshot_publication_started",
        "snapshot_publication_failed",
    ]
    assert events[1]["error_type"] == "RuntimeError"
    assert "vendor failed" not in caplog.text


def test_console_entrypoint_suppresses_failure_traceback(monkeypatch, capsys) -> None:
    def fail(arguments: list[str], *, settings: Settings) -> str:
        raise RuntimeError("vendor failed: credential secret")

    monkeypatch.setattr(snapshot_ingestion, "run_snapshot_ingestion", fail)
    monkeypatch.setattr(snapshot_ingestion, "get_settings", lambda: Settings(None))
    monkeypatch.setattr("sys.argv", ["refinvest-publish-snapshot"])

    with pytest.raises(SystemExit, match="1"):
        snapshot_ingestion.snapshot_ingestion_main()

    captured = capsys.readouterr()
    assert "credential secret" not in captured.out
    assert "credential secret" not in captured.err


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
        self.start: date | None = None
        self.end: date | None = None

    def publish(
        self,
        request: MvpSnapshotRequest,
        start: date,
        end: date,
        overrides: dict[date, datetime | None],
    ) -> PublishedSnapshotArtifacts:
        self.request = request
        self.overrides = overrides
        self.start = start
        self.end = end
        return PublishedSnapshotArtifacts(
            request.snapshot_id, f"published/{request.snapshot_id}", "x", "y"
        )


class FailingCoordinator:
    def publish(
        self,
        request: MvpSnapshotRequest,
        start: date,
        end: date,
        overrides: dict[date, datetime | None],
    ) -> PublishedSnapshotArtifacts:
        raise RuntimeError("vendor failed: credential secret")
