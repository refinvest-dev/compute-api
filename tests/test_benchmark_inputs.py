import hashlib
import json
from pathlib import Path

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionData
from refinvest.benchmarking.inputs import (
    BenchmarkInputError,
    VerifiedFixtureLoader,
    load_fixture,
    load_scenario,
)


def test_loads_version_one_integration_manifest_and_scenario(tmp_path: Path) -> None:
    manifest, snapshot = _fixture(tmp_path)
    fixture = load_fixture(manifest, snapshot)
    loaded = load_scenario(_scenario(tmp_path), fixture.fixture_id)

    assert fixture.fixture_id == "performance-mvp-daily-v1"
    assert fixture.manifest_sha256.startswith("sha256:")
    assert loaded.scenario_id == "simple-test"


@pytest.mark.parametrize(
    ("field", "value"),
    (("schemaVersion", 2), ("fixtureDefinitionVersion", 2)),
)
def test_rejects_unsupported_fixture_versions(tmp_path: Path, field: str, value: int) -> None:
    manifest, snapshot = _fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkInputError):
        load_fixture(manifest, snapshot)


def test_rejects_path_traversal_and_invalid_artifact_hash(tmp_path: Path) -> None:
    manifest, snapshot = _fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifacts"][0]["name"] = "../calendar.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkInputError):
        load_fixture(manifest, snapshot)

    payload["artifacts"][0]["name"] = "calendar.json"
    payload["artifacts"][0]["sha256"] = "ABC"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkInputError):
        load_fixture(manifest, snapshot)


@pytest.mark.parametrize("mutation", ("missing", "hash"))
def test_verified_loader_rejects_missing_or_changed_declared_artifact(
    tmp_path: Path, mutation: str
) -> None:
    manifest, snapshot_path = _fixture(tmp_path)
    fixture = load_fixture(manifest, snapshot_path)
    artifact = snapshot_path / "calendar.json"
    if mutation == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"changed")

    with pytest.raises(BenchmarkInputError):
        VerifiedFixtureLoader(fixture, NeverLoader()).load(fixture.snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    (("scenarioVersion", 2), ("canonicalizationVersion", 2), ("fixtureId", "other")),
)
def test_rejects_scenario_version_and_fixture_mismatches(
    tmp_path: Path, field: str, value: object
) -> None:
    path = _scenario(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkInputError):
        load_scenario(path, "performance-mvp-daily-v1")


def test_rejects_invalid_transport_request_without_echoing_it(tmp_path: Path) -> None:
    path = _scenario(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["request"]["strategyVersion"]["conditions"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkInputError, match="transport/domain") as raised:
        load_scenario(path, "performance-mvp-daily-v1")
    assert "conditions" not in str(raised.value)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    entries = []
    for name in ("calendar.json", "daily-bars.ndjson", "manifest.json"):
        content = name.encode()
        (snapshot / name).write_bytes(content)
        entries.append(
            {
                "name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "sizeBytes": len(content),
            }
        )
    manifest_hash = next(item["sha256"] for item in entries if item["name"] == "manifest.json")
    payload = {
        "schemaVersion": 1,
        "fixtureId": "performance-mvp-daily-v1",
        "fixtureDefinitionVersion": 1,
        "createdAt": "2025-01-01T00:00:00Z",
        "period": {"start": "2015-01-01", "end": "2024-12-31"},
        "frequency": "DAILY_BAR",
        "assets": ["QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT"],
        "adjustmentPolicy": "adjusted",
        "calendar": {"source": "test", "version": "1", "contentHash": "hash"},
        "generator": {"name": "test-generator"},
        "artifacts": entries,
        "storageIndependentManifestSha256": manifest_hash,
    }
    path = tmp_path / "fixture-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, snapshot


def _scenario(tmp_path: Path) -> Path:
    payload = {
        "scenarioId": "simple-test",
        "scenarioVersion": 1,
        "fixtureId": "performance-mvp-daily-v1",
        "cacheModes": ["COLD", "WARM"],
        "canonicalizationVersion": 1,
        "request": {
            "strategyVersionId": "strategy-v1",
            "strategyVersion": {
                "primarySignalAsset": "QQQ",
                "conditions": [
                    {
                        "operator": "LT",
                        "logicalCombinator": None,
                        "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": None},
                        "operandB": 0,
                    }
                ],
                "executionAsset": "QQQ",
                "lag": 0,
                "exit": {"holdingSignalSessions": 1},
                "positionPolicy": {
                    "longOnly": True,
                    "singlePosition": True,
                    "duplicateEntry": "IGNORE",
                },
            },
            "feeModel": {"commission": 0, "slippage": 0},
            "period": {"start": "2024-01-01", "end": "2024-01-02"},
            "datasetSnapshotId": "performance-mvp-daily-v1",
        },
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class NeverLoader:
    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        raise AssertionError(f"delegate must not run: {snapshot.snapshot_id}")
