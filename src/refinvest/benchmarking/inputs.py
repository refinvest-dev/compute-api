"""Adapters for the integration-owned performance manifest and scenarios."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from refinvest.api.schemas.backtests import CreateBacktestRequestTransport
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import (
    SnapshotExecutionData,
    SnapshotExecutionDataLoader,
)
from refinvest.engine.validation import StrategyValidationError, validate_strategy_version

_ARTIFACT_NAMES = frozenset({"calendar.json", "daily-bars.ndjson", "manifest.json"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class BenchmarkInputError(ValueError):
    """An integration-owned benchmark input is unsupported or invalid."""


@dataclass(frozen=True)
class ArtifactExpectation:
    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class FixtureInput:
    manifest_path: Path
    snapshot_path: Path
    fixture_id: str
    version: int
    manifest_sha256: str
    snapshot: DatasetSnapshot
    artifacts: tuple[ArtifactExpectation, ...]


@dataclass(frozen=True)
class ScenarioInput:
    path: Path
    scenario_id: str
    version: int
    fixture_id: str
    canonicalization_version: int
    request_payload: dict[str, object]


def load_fixture(manifest_path: Path, snapshot_path: Path) -> FixtureInput:
    manifest_path = manifest_path.resolve()
    snapshot_path = snapshot_path.resolve()
    payload = _json_object(manifest_path)
    if payload.get("schemaVersion") != 1:
        raise BenchmarkInputError("Unsupported fixture manifest schemaVersion.")
    version = _integer(payload, "fixtureDefinitionVersion")
    if version != 1:
        raise BenchmarkInputError("Unsupported fixture definition version.")
    fixture_id = _string(payload, "fixtureId")
    if payload.get("frequency") != "DAILY_BAR":
        raise BenchmarkInputError("Fixture manifest must describe Daily Bar data.")
    assets = payload.get("assets")
    if assets != ["QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT"]:
        raise BenchmarkInputError("Fixture manifest has an unsupported asset universe.")
    created_at = _utc_datetime(payload, "createdAt")
    period = _mapping(payload, "period")
    calendar = _mapping(payload, "calendar")
    generator = _mapping(payload, "generator")
    artifacts = _artifact_expectations(payload)
    storage_hash = _string(payload, "storageIndependentManifestSha256")
    if not _SHA256_PATTERN.fullmatch(storage_hash):
        raise BenchmarkInputError("Fixture storage-independent manifest hash is invalid.")
    snapshot = DatasetSnapshot(
        snapshot_id=fixture_id,
        created_at=created_at,
        source=_string(generator, "name"),
        coverage={
            "firstDate": _iso_date(period, "start").isoformat(),
            "lastDate": _iso_date(period, "end").isoformat(),
        },
        adjustment_policy=_string(payload, "adjustmentPolicy"),
        storage_path=str(snapshot_path),
        calendar_source=_string(calendar, "source"),
        calendar_version=_string(calendar, "version"),
        calendar_hash=_string(calendar, "contentHash"),
    )
    return FixtureInput(
        manifest_path,
        snapshot_path,
        fixture_id,
        version,
        f"sha256:{storage_hash}",
        snapshot,
        artifacts,
    )


def load_scenario(path: Path, fixture_id: str) -> ScenarioInput:
    resolved = path.resolve()
    payload = _json_object(resolved)
    version = _integer(payload, "scenarioVersion")
    if version != 1:
        raise BenchmarkInputError("Unsupported scenario version.")
    canonicalization_version = _integer(payload, "canonicalizationVersion")
    if canonicalization_version != 1:
        raise BenchmarkInputError("Unsupported scenario canonicalization version.")
    scenario_fixture_id = _string(payload, "fixtureId")
    if scenario_fixture_id != fixture_id:
        raise BenchmarkInputError("Scenario fixtureId does not match the fixture manifest.")
    request = _mapping(payload, "request")
    request_snapshot_id = request.get("datasetSnapshotId")
    if request_snapshot_id != fixture_id:
        raise BenchmarkInputError("Scenario request does not pin the fixture DatasetSnapshot.")
    cache_modes = payload.get("cacheModes")
    if cache_modes != ["COLD", "WARM"]:
        raise BenchmarkInputError("Scenario cacheModes must be the version 1 COLD/WARM series.")
    try:
        transport = CreateBacktestRequestTransport.model_validate(request)
        validate_strategy_version(transport.to_domain().strategy_version)
    except (ValidationError, StrategyValidationError, TypeError, ValueError) as error:
        raise BenchmarkInputError("Scenario request failed transport/domain validation.") from error
    return ScenarioInput(
        resolved,
        _string(payload, "scenarioId"),
        version,
        scenario_fixture_id,
        canonicalization_version,
        request,
    )


class VerifiedFixtureLoader:
    """Recheck the integration manifest and declared artifacts before the Compute loader."""

    def __init__(self, fixture: FixtureInput, delegate: SnapshotExecutionDataLoader) -> None:
        self._fixture = fixture
        self._delegate = delegate

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        current = load_fixture(self._fixture.manifest_path, self._fixture.snapshot_path)
        if current != self._fixture:
            raise BenchmarkInputError("Fixture manifest changed after benchmark preparation.")
        for artifact in current.artifacts:
            path = _safe_artifact_path(current.snapshot_path, artifact.name)
            try:
                content = path.read_bytes()
            except OSError as error:
                raise BenchmarkInputError(
                    f"Declared fixture artifact is unavailable: {artifact.name}"
                ) from error
            if len(content) != artifact.size_bytes:
                raise BenchmarkInputError(f"Fixture artifact size mismatch: {artifact.name}")
            if hashlib.sha256(content).hexdigest() != artifact.sha256:
                raise BenchmarkInputError(f"Fixture artifact SHA-256 mismatch: {artifact.name}")
        manifest_artifact = next(item for item in current.artifacts if item.name == "manifest.json")
        if f"sha256:{manifest_artifact.sha256}" != current.manifest_sha256:
            raise BenchmarkInputError("Storage-independent manifest hash mismatch.")
        return self._delegate.load(snapshot)


def _artifact_expectations(payload: dict[str, object]) -> tuple[ArtifactExpectation, ...]:
    raw = payload.get("artifacts")
    if not isinstance(raw, list):
        raise BenchmarkInputError("Fixture artifacts must be an array.")
    entries: list[ArtifactExpectation] = []
    for value in cast(list[object], raw):
        if not isinstance(value, dict):
            raise BenchmarkInputError("Fixture artifact entries must be objects.")
        item = cast(dict[str, object], value)
        name = _string(item, "name")
        _validate_relative_artifact_name(name)
        digest = _string(item, "sha256")
        if not _SHA256_PATTERN.fullmatch(digest):
            raise BenchmarkInputError(f"Fixture artifact SHA-256 is invalid: {name}")
        size = _integer(item, "sizeBytes")
        if size < 0:
            raise BenchmarkInputError(f"Fixture artifact size is invalid: {name}")
        entries.append(ArtifactExpectation(name, digest, size))
    if frozenset(item.name for item in entries) != _ARTIFACT_NAMES or len(entries) != len(
        _ARTIFACT_NAMES
    ):
        raise BenchmarkInputError("Fixture manifest must declare exactly the Compute artifacts.")
    return tuple(entries)


def _safe_artifact_path(root: Path, name: str) -> Path:
    _validate_relative_artifact_name(name)
    candidate = (root / Path(*PurePosixPath(name).parts)).resolve()
    if candidate.parent != root:
        raise BenchmarkInputError("Fixture artifact escapes the snapshot root.")
    return candidate


def _validate_relative_artifact_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or name != path.as_posix() or len(path.parts) != 1 or ".." in path.parts:
        raise BenchmarkInputError("Fixture artifact paths must be normalized relative names.")


def _json_object(path: Path) -> dict[str, object]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchmarkInputError(
            f"Benchmark JSON input is unavailable or invalid: {path.name}"
        ) from error
    if not isinstance(value, dict):
        raise BenchmarkInputError(f"Benchmark JSON input must be an object: {path.name}")
    return cast(dict[str, object], value)


def _mapping(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise BenchmarkInputError(f"Benchmark input field must be an object: {field}")
    return cast(dict[str, object], value)


def _string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise BenchmarkInputError(f"Benchmark input field must be a non-empty string: {field}")
    return value


def _integer(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BenchmarkInputError(f"Benchmark input field must be an integer: {field}")
    return value


def _iso_date(payload: dict[str, object], field: str) -> date:
    try:
        return date.fromisoformat(_string(payload, field))
    except ValueError as error:
        raise BenchmarkInputError(f"Benchmark input field must be an ISO date: {field}") from error


def _utc_datetime(payload: dict[str, object], field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_string(payload, field).replace("Z", "+00:00"))
    except ValueError as error:
        raise BenchmarkInputError(
            f"Benchmark input field must be a UTC timestamp: {field}"
        ) from error
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise BenchmarkInputError(f"Benchmark input field must be a UTC timestamp: {field}")
    return parsed.astimezone(UTC)
