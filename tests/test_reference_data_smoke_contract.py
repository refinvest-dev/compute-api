"""Characterize snapshot selection, diagnostics and the deployed console contract."""

import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from importlib import import_module
from pathlib import Path

import pytest

from refinvest.app import integration_fixture
from refinvest.app.config import Settings
from refinvest.app.reference_data_smoke import verify_reference_data
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import (
    SnapshotArtifactError,
    SnapshotExecutionData,
)
from refinvest.ingestion.snapshot.artifacts import FilesystemSnapshotArtifactPublisher
from refinvest.marketdata.domain import ClosePriceLookup, DailyBarAvailability
from refinvest.marketdata.ports import MissingRequiredMarketDataError
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)


class DiagnosticInputs:
    def __init__(self) -> None:
        self.snapshot = DatasetSnapshot(
            "fixed", datetime(2025, 1, 6, tzinfo=UTC), "test", {}, "adjusted", "unused"
        )
        self.settings = Settings(None, database_url="postgresql://example")
        self.events: list[object] = []
        self.loaded: list[DatasetSnapshot] = []
        self.present = True
        self.absent_asset: str | None = None
        self.crypto_dates = (date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5))
        self.missing_close = False
        self.load_error: Exception | None = None

    def repository(self, database_url: str):
        assert database_url == self.settings.database_url
        self.events.append("repository")
        return self

    def latest(self):
        self.events.append("latest")
        return self.snapshot if self.present else None

    def get(self, snapshot_id: str):
        self.events.append(("get", snapshot_id))
        return self.snapshot if self.present and snapshot_id == self.snapshot.snapshot_id else None

    def loader(self, settings: Settings):
        assert settings is self.settings
        self.events.append("loader")
        return self

    def load(self, snapshot: DatasetSnapshot):
        self.loaded.append(snapshot)
        if self.load_error is not None:
            raise self.load_error
        return SnapshotExecutionData(snapshot.snapshot_id, self, self, True, True)

    def expected_sessions(self, asset: str):
        self.events.append(("sessions", asset))
        if asset == self.absent_asset:
            return ()
        if asset == "BTCUSDT":
            return self.crypto_dates
        return (date(2025, 1, 2), date(2025, 1, 3))

    def get_close(self, asset: str, session_date: date):
        self.events.append(("close", asset, session_date))
        if self.missing_close:
            return ClosePriceLookup(DailyBarAvailability.MISSING_EXPECTED_SESSION)
        return ClosePriceLookup(DailyBarAvailability.AVAILABLE, Decimal("100"))

    def verify(self, snapshot_id: str | None = None):
        return verify_reference_data(
            self.settings,
            snapshot_id,
            snapshot_repository_factory=self.repository,
            loader_factory=self.loader,
        )


@pytest.mark.parametrize("snapshot_id", [None, "", "fixed"])
def test_selects_once_before_loader_and_reuses_the_same_snapshot(snapshot_id: str | None) -> None:
    inputs = DiagnosticInputs()
    result = inputs.verify(snapshot_id)

    selection = ("get", "fixed") if snapshot_id else "latest"
    assert inputs.events[:3] == ["repository", selection, "loader"]
    assert inputs.events.count(selection) == 1
    assert len(inputs.loaded) == 7
    assert all(snapshot is inputs.snapshot for snapshot in inputs.loaded)
    assert result == {
        "status": "ok",
        "datasetSnapshotId": "fixed",
        "snapshotCreatedAt": datetime(2025, 1, 6, tzinfo=UTC),
        "adjustmentPolicy": "adjusted",
        "availabilitySymbols": ["QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT"],
        "seriesSymbols": ["QQQ", "BTCUSDT"],
        "seriesStart": date(2024, 12, 27),
        "seriesEnd": date(2025, 1, 3),
    }


@pytest.mark.parametrize("snapshot_id", [None, "missing"])
def test_unavailable_snapshot_never_creates_loader_or_falls_back(snapshot_id: str | None) -> None:
    inputs = DiagnosticInputs()
    if snapshot_id is None:
        inputs.present = False
    with pytest.raises(ValueError, match="^The requested DatasetSnapshot is unavailable\\.$"):
        inputs.verify(snapshot_id)

    selection = ("get", "missing") if snapshot_id else "latest"
    assert inputs.events == ["repository", selection]
    assert not inputs.loaded


@pytest.mark.parametrize("database_url", [None, ""])
def test_database_validation_precedes_repository_and_loader(database_url: str | None) -> None:
    inputs = DiagnosticInputs()
    inputs.settings = Settings(None, database_url=database_url)
    with pytest.raises(ValueError, match="REFINVEST_DATABASE_URL"):
        inputs.verify()
    assert not inputs.events


def test_missing_catalog_coverage_checks_all_five_assets_then_stops() -> None:
    inputs = DiagnosticInputs()
    inputs.absent_asset = "SOXL"
    with pytest.raises(ValueError, match="lacks a supported asset's calendar coverage"):
        inputs.verify()
    assert inputs.events[3:] == [
        ("sessions", asset) for asset in ("QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT")
    ]
    assert len(inputs.loaded) == 5


def test_empty_recent_series_fails_without_synthesizing_cross_calendar_points() -> None:
    inputs = DiagnosticInputs()
    inputs.crypto_dates = (date(2025, 1, 20),)
    with pytest.raises(ValueError, match="lacks recent cross-calendar series points"):
        inputs.verify()
    assert not any(event[:2] == ("close", "BTCUSDT") for event in inputs.events)


def test_missing_expected_close_remains_a_fatal_error() -> None:
    inputs = DiagnosticInputs()
    inputs.missing_close = True
    with pytest.raises(MissingRequiredMarketDataError, match="expected close price"):
        inputs.verify()


def test_artifact_failure_propagates_without_retry_or_snapshot_reselection() -> None:
    inputs = DiagnosticInputs()
    error = SnapshotArtifactError("invalid immutable manifest")
    inputs.load_error = error
    with pytest.raises(SnapshotArtifactError) as caught:
        inputs.verify()
    assert caught.value is error
    assert inputs.events == ["repository", "latest", "loader"]
    assert inputs.loaded == [inputs.snapshot]


@pytest.mark.parametrize("option", [[], ["--dataset-snapshot-id", "fixed"]])
def test_console_keeps_one_compact_json_line(
    option: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = import_module("refinvest.app.reference_data_smoke")
    inputs = DiagnosticInputs()
    monkeypatch.setattr(sys, "argv", ["refinvest-verify-reference-data", *option])
    monkeypatch.setattr(module, "get_settings", lambda: inputs.settings)
    selected: list[str | None] = []

    def verify(settings: Settings, snapshot_id: str | None):
        assert settings is inputs.settings
        selected.append(snapshot_id)
        return inputs.verify(snapshot_id)

    monkeypatch.setattr(module, "verify_reference_data", verify)
    module.reference_data_smoke_main()
    output = capsys.readouterr()
    assert selected == (["fixed"] if option else [None])
    assert output.err == ""
    result = json.loads(output.out)
    assert output.out == json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n"
    assert result["snapshotCreatedAt"] == "2025-01-06 00:00:00+00:00"
    assert result["seriesStart"] == "2024-12-27"
    assert result["seriesEnd"] == "2025-01-03"


@pytest.mark.parametrize("error", [ValueError("private credential"), RuntimeError("private path")])
@pytest.mark.parametrize("failure_at", ["settings", "verification"])
def test_console_failure_keeps_exit_one_without_details(
    error: Exception,
    failure_at: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = import_module("refinvest.app.reference_data_smoke")
    monkeypatch.setattr(sys, "argv", ["refinvest-verify-reference-data"])

    def fail(*_):
        raise error

    monkeypatch.setattr(module, "get_settings", lambda: Settings(None))
    monkeypatch.setattr(
        module, "get_settings" if failure_at == "settings" else "verify_reference_data", fail
    )
    with pytest.raises(SystemExit) as caught:
        module.reference_data_smoke_main()
    assert caught.value.code == 1
    assert caught.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("option,code", [(["--help"], 0), (["--unknown"], 2)])
def test_parser_exit_does_not_read_configuration(
    option: list[str], code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = import_module("refinvest.app.reference_data_smoke")
    monkeypatch.setattr(sys, "argv", ["refinvest-verify-reference-data", *option])

    def fail():
        pytest.fail("Parser exit must not load configuration")

    monkeypatch.setattr(module, "get_settings", fail)
    with pytest.raises(SystemExit) as caught:
        module.reference_data_smoke_main()
    assert caught.value.code == code


def verify_filesystem_snapshot(root: Path) -> dict[str, object]:
    """Exercise real immutable files without a DB or vendor account."""
    inputs = DiagnosticInputs()
    provenance = integration_fixture._CALENDAR_PROVENANCE
    inputs.snapshot = DatasetSnapshot(
        "diagnostic-fixed",
        integration_fixture._CREATED_AT,
        "integration-fixture",
        {"firstDate": "2025-01-02", "lastDate": "2025-01-08"},
        "split-adjusted-ohlcv-cash-dividends-unadjusted",
        str(root / "diagnostic-fixed"),
        provenance.source,
        provenance.version,
        provenance.content_hash,
    )
    FilesystemSnapshotArtifactPublisher().publish(
        inputs.snapshot,
        provenance,
        integration_fixture._sessions(),
        integration_fixture._bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )

    def hashes():
        return {
            file.name: sha256(file.read_bytes()).hexdigest()
            for file in sorted(Path(inputs.snapshot.storage_path).iterdir())
        }

    before = hashes()
    results = [
        verify_reference_data(
            inputs.settings,
            snapshot_id,
            snapshot_repository_factory=inputs.repository,
            loader_factory=lambda _: FilesystemSnapshotExecutionDataLoader(),
        )
        for snapshot_id in (None, "diagnostic-fixed")
    ]
    assert results[0] == results[1]
    assert hashes() == before
    return {"result": results[0], "hashes": before}


def test_filesystem_diagnostic_reads_without_mutating_snapshot_artifacts(tmp_path: Path) -> None:
    evidence = verify_filesystem_snapshot(tmp_path)
    result = evidence["result"]
    assert result["datasetSnapshotId"] == "diagnostic-fixed"
    assert result["seriesStart"] == date(2025, 1, 1)
    assert result["seriesEnd"] == date(2025, 1, 8)
    assert set(evidence["hashes"]) == {"calendar.json", "daily-bars.ndjson", "manifest.json"}
