import json
from datetime import UTC, datetime
from pathlib import Path

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.benchmarking.inputs import FixtureInput, ScenarioInput
from refinvest.benchmarking.measurement import REQUIRED_STAGES, IterationResult, MeasurementResult
from refinvest.benchmarking.report import build_report, write_report_atomic


def test_expected_mismatch_is_machine_readable_and_atomic_report_is_written(
    tmp_path: Path,
) -> None:
    fixture, scenario = _identity(tmp_path)
    sample = IterationResult(
        1,
        "SUCCEEDED",
        100,
        {stage: 1 for stage in REQUIRED_STAGES},
        50,
        "sha256:" + "a" * 64,
        "COMPLETED",
        0,
        "ZERO",
    )
    measurement = MeasurementResult((sample,), sample.result_hash, "COMPLETED", 0, "ZERO", True, ())
    report, passed = build_report(
        fixture,
        scenario,
        measurement,
        cache_mode="warm",
        warmups=0,
        iterations=1,
        engine_version="engine-v1",
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        completed_at=datetime(2025, 1, 1, tzinfo=UTC),
        expected={
            "fixtureId": fixture.fixture_id,
            "scenarioId": scenario.scenario_id,
            "canonicalizationVersion": 1,
            "terminalStatus": "COMPLETED",
            "tradeCount": 1,
            "sampleSizeWarning": "LOW",
            "resultHash": "sha256:" + "b" * 64,
        },
        repository_root=tmp_path,
    )

    assert not passed
    assert report["reportVersion"] == 1
    assert report["summary"]["total"]["p95Nanoseconds"] == 100  # type: ignore[index]
    fields = {item["field"] for item in report["correctness"]["mismatches"]}  # type: ignore[index]
    assert fields == {"tradeCount", "sampleSizeWarning", "resultHash"}
    destination = tmp_path / "nested" / "report.json"
    write_report_atomic(destination, report)
    assert json.loads(destination.read_text(encoding="utf-8"))["correctness"]["passed"] is False
    assert not list(destination.parent.glob("*.tmp"))


def _identity(tmp_path: Path) -> tuple[FixtureInput, ScenarioInput]:
    snapshot = DatasetSnapshot(
        "fixture-v1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        str(tmp_path),
    )
    fixture = FixtureInput(
        tmp_path / "manifest.json",
        tmp_path,
        snapshot.snapshot_id,
        1,
        "sha256:" + "0" * 64,
        snapshot,
        (),
    )
    scenario = ScenarioInput(
        tmp_path / "scenario.json", "scenario-v1", 1, fixture.fixture_id, 1, {}
    )
    return fixture, scenario
