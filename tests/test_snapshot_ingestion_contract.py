"""Publication validation, lifecycle and artifact contracts across CLI extraction."""

import hashlib
import json
import logging
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from refinvest.app.config import Settings
from refinvest.app.snapshot_ingestion import run_snapshot_ingestion
from refinvest.ingestion.snapshot.artifacts import (
    FilesystemSnapshotArtifactPublisher,
    PublishedSnapshotArtifacts,
    SnapshotPublicationError,
)
from refinvest.ingestion.snapshot.mvp import MvpSnapshotIngestionCoordinator
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)
from tests.test_mvp_snapshot_ingestion_coordinator import FakeBtcusdtBars, FakeEtfBars
from tests.test_snapshot_ingestion_cli import FakeCoordinator

LOGGER = "refinvest.app.snapshot_ingestion"
NOW = datetime(2025, 1, 3, tzinfo=UTC)


@pytest.fixture
def publication_module():
    return import_module("refinvest.ingestion.snapshot.cli")


@pytest.fixture
def publication(tmp_path):
    overrides = tmp_path / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")
    state = SimpleNamespace(
        overrides=overrides,
        settings=Settings(
            None,
            snapshot_storage_root=str(tmp_path / "snapshots"),
            snapshot_start_date="2025-01-02",
            us_equity_overrides_path=str(overrides),
        ),
        arguments=["--snapshot-id", "snapshot-fixed"],
        calls=[],
        coordinator=FakeCoordinator(),
    )

    def factory(settings):
        assert settings is state.settings
        state.calls.append("factory")
        return state.coordinator

    state.factory = factory
    return state


def invoke(publication, *, arguments=None, **changes):
    options = dict(
        settings=publication.settings,
        now=lambda: NOW,
        snapshot_nonce=lambda: "fixed",
        coordinator_factory=publication.factory,
    )
    options.update(changes)
    return run_snapshot_ingestion(
        publication.arguments if arguments is None else arguments, **options
    )


@pytest.mark.parametrize("zone", [None, timezone(timedelta(hours=9))])
def test_invalid_clock_fails_before_coordinator_and_publication_events(publication, zone, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    with pytest.raises(ValueError, match="exact UTC"):
        invoke(publication, now=lambda: datetime(2025, 1, 3, tzinfo=zone))
    assert publication.calls == []
    assert caplog.records == []


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "readable UTF-8 JSON file"),
        ("[]", "JSON object"),
        ('{"bad-date":null}', "ISO date"),
        ('{"2025-01-02":1}', "timestamp or null"),
        ('{"2025-01-02":"bad-time"}', "ISO-8601"),
        ('{"2025-01-02":"2025-01-02T18:00:00+09:00"}', "exact UTC"),
    ],
)
def test_override_errors_do_not_construct_coordinator_or_emit_publication_events(
    publication, caplog, content, message
):
    publication.overrides.write_text(content, encoding="utf-8")
    caplog.set_level(logging.INFO, logger=LOGGER)
    with pytest.raises(ValueError, match=message):
        invoke(publication)
    assert publication.calls == []
    assert caplog.records == []


def test_unreadable_overrides_fail_before_storage_resolution(publication):
    publication.settings = replace(publication.settings, snapshot_storage_backend="unsupported")
    with pytest.raises(ValueError, match="readable UTF-8 JSON file"):
        invoke(
            publication, arguments=[*publication.arguments, "--us-equity-overrides", "missing.json"]
        )
    assert publication.calls == []


@pytest.mark.parametrize(("arguments", "code"), [(["--help"], 0), (["--unknown"], 2)])
def test_parser_exit_does_not_read_clock_or_construct_coordinator(publication, arguments, code):
    def forbidden():
        raise AssertionError("clock must not be read")

    with pytest.raises(SystemExit) as caught:
        invoke(publication, arguments=arguments, now=forbidden)
    assert caught.value.code == code
    assert publication.calls == []


def test_completed_event_and_stdout_use_the_published_identity(
    publication, publication_module, monkeypatch, caplog
):
    ticks = iter([10.0, 11.25])
    monkeypatch.setattr(publication_module, "monotonic", lambda: next(ticks))
    publication.coordinator = SimpleNamespace(
        publish=lambda *_: PublishedSnapshotArtifacts(
            "published-other", "store/actual", "calendar", "bars"
        )
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert invoke(publication) == '{"snapshotId":"published-other","storagePath":"store/actual"}'
    events = [json.loads(record.message) for record in caplog.records]
    assert events == [
        {
            "event": "snapshot_publication_started",
            "snapshot_id": "snapshot-fixed",
            "coverage_start": "2025-01-02",
            "coverage_end": "2025-01-02",
            "storage_path": str(
                Path(publication.settings.snapshot_storage_root) / "snapshot-fixed"
            ),
        },
        {
            "event": "snapshot_publication_completed",
            "snapshot_id": "published-other",
            "coverage_start": "2025-01-02",
            "coverage_end": "2025-01-02",
            "storage_path": "store/actual",
            "publication_duration_seconds": 1.25,
        },
    ]
    assert [record.name for record in caplog.records] == [LOGGER, LOGGER]
    assert publication.calls == ["factory"]


def test_factory_failure_keeps_started_failed_order_and_same_exception(
    publication, publication_module, monkeypatch, caplog
):
    error = RuntimeError("private vendor credential")

    def fail(_):
        raise error

    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(publication_module, "monotonic", lambda: next(ticks))
    caplog.set_level(logging.INFO, logger=LOGGER)
    with pytest.raises(RuntimeError) as caught:
        invoke(publication, coordinator_factory=fail)
    assert caught.value is error
    events = [json.loads(record.message) for record in caplog.records]
    assert [event["event"] for event in events] == [
        "snapshot_publication_started",
        "snapshot_publication_failed",
    ]
    assert events[1]["error_type"] == "RuntimeError"
    assert events[1]["publication_duration_seconds"] == 0.25
    assert "private vendor credential" not in caplog.text
    assert caplog.records[1].levelno == logging.WARNING


def test_explicit_options_override_defaults_without_consuming_nonce(publication):
    publication.overrides.write_text('{"2025-01-02":null}', encoding="utf-8")
    publication.settings = replace(
        publication.settings, snapshot_start_date="invalid", us_equity_overrides_path="missing.json"
    )

    def forbidden():
        raise AssertionError("explicit ID must not consume nonce")

    invoke(
        publication,
        arguments=[
            *publication.arguments,
            "--start",
            "2025-01-02",
            "--end",
            "2025-01-02",
            "--us-equity-overrides",
            str(publication.overrides),
        ],
        snapshot_nonce=forbidden,
    )
    assert publication.coordinator.overrides == {date(2025, 1, 2): None}
    assert publication.coordinator.request.snapshot_id == "snapshot-fixed"
    assert publication.calls == ["factory"]


def test_console_prints_one_result_line(publication, monkeypatch, capsys):
    app = import_module("refinvest.app.snapshot_ingestion")
    monkeypatch.setattr(app, "get_settings", lambda: publication.settings)
    monkeypatch.setattr(
        app, "run_snapshot_ingestion", lambda args, **kwargs: '{"snapshotId":"fixed"}'
    )
    monkeypatch.setattr("sys.argv", ["refinvest-publish-snapshot"])
    app.snapshot_ingestion_main()
    captured = capsys.readouterr()
    assert captured.out == '{"snapshotId":"fixed"}\n'
    assert captured.err == ""


def publish_offline(root: Path):
    from refinvest.ingestion.calendars.crypto_utc import build_crypto_utc_calendar
    from refinvest.ingestion.calendars.us_equity import generate_us_equity_calendar

    root.mkdir(parents=True, exist_ok=True)
    overrides = root / "overrides.json"
    overrides.write_text("{}", encoding="utf-8")
    snapshots = []

    def record(snapshot):
        loaded = FilesystemSnapshotExecutionDataLoader().load(snapshot)
        assert loaded.snapshot_id == snapshot.snapshot_id
        assert loaded.calendar.expected_sessions("QQQ") == (date(2025, 1, 2),)
        assert loaded.calendar.expected_sessions("BTCUSDT") == (date(2025, 1, 2),)
        assert loaded.corporate_actions_applied and loaded.point_in_time_validation_passed
        snapshots.append(snapshot)

    coordinator = MvpSnapshotIngestionCoordinator(
        generate_us_equity_calendar,
        build_crypto_utc_calendar,
        FakeEtfBars(),
        FakeBtcusdtBars(),
        FilesystemSnapshotArtifactPublisher(),
        SimpleNamespace(create=record),
    )
    result = run_snapshot_ingestion(
        ["--snapshot-id", "offline-fixed"],
        settings=Settings(
            None,
            snapshot_storage_root=str(root / "snapshots"),
            snapshot_start_date="2025-01-02",
            us_equity_overrides_path=str(overrides),
        ),
        now=lambda: NOW,
        coordinator_factory=lambda _: coordinator,
    )
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    metadata = asdict(snapshot)
    metadata.pop("storage_path")
    directory = Path(snapshot.storage_path)
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
    return {"metadata": metadata, "hashes": hashes, "snapshot_id": json.loads(result)["snapshotId"]}


def test_offline_cli_publishes_verified_immutable_artifacts_before_metadata(tmp_path):
    result = publish_offline(tmp_path)
    assert result["snapshot_id"] == "offline-fixed"
    assert set(result["hashes"]) == {"manifest.json", "calendar.json", "daily-bars.ndjson"}
    assert result["metadata"]["source"] == "BINANCE_PUBLIC_DATA"
    assert result["metadata"]["coverage"] == {"firstDate": "2025-01-02", "lastDate": "2025-01-02"}
    with pytest.raises(SnapshotPublicationError, match="already exists"):
        publish_offline(tmp_path)
