"""Filesystem reader for immutable, manifest-verified DatasetSnapshot artifacts."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import cast

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import (
    SnapshotArtifactError,
    SnapshotExecutionData,
)
from refinvest.ingestion.snapshot.calendar import CalendarComponent, validate_composite_calendar
from refinvest.marketdata.adapters.in_memory import (
    InMemorySnapshotCalendar,
    InMemorySnapshotDailyBars,
    InMemorySnapshotReferenceCloses,
    SnapshotMarketData,
)
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
    ReferenceClose,
)


class FilesystemSnapshotExecutionDataLoader:
    """Load only manifest-declared artifacts from one pinned snapshot directory."""

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        root = Path(snapshot.storage_path)
        return load_snapshot_execution_data(snapshot, lambda name: _read_file(root / name))


def load_snapshot_execution_data(
    snapshot: DatasetSnapshot, read_artifact: Callable[[Path], bytes]
) -> SnapshotExecutionData:
    """Reconstruct one pinned snapshot from a manifest-safe artifact reader."""
    manifest = _json_object(read_artifact(Path("manifest.json")), "manifest.json")
    _require_equal(_string(manifest, "snapshotId"), snapshot.snapshot_id, "manifest snapshotId")
    format_version = _integer(manifest, "formatVersion")
    if format_version not in {1, 2, 3}:
        raise SnapshotArtifactError("Unexpected manifest formatVersion.")
    calendar_bytes = _read_declared_artifact(read_artifact, _object(manifest, "calendar"))
    daily_bars_bytes = _read_declared_artifact(read_artifact, _object(manifest, "dailyBars"))
    reference_closes_bytes = (
        _read_declared_artifact(read_artifact, _object(manifest, "referenceCloses"))
        if "referenceCloses" in manifest
        else None
    )
    calendar = _calendar_from_json(snapshot, calendar_bytes)
    daily_bars = _daily_bars_from_ndjson(snapshot.snapshot_id, daily_bars_bytes, format_version)
    reference_closes = _reference_closes_from_ndjson(snapshot.snapshot_id, reference_closes_bytes)
    market_data = SnapshotMarketData(snapshot, calendar, daily_bars, reference_closes)
    return SnapshotExecutionData(
        snapshot.snapshot_id,
        calendar,
        market_data,
        _boolean(manifest, "corporateActionsApplied"),
        _boolean(manifest, "pointInTimeValidationPassed"),
    )


def _read_declared_artifact(
    read_artifact: Callable[[Path], bytes], entry: dict[str, object]
) -> bytes:
    relative_path = _safe_relative_path(_string(entry, "path"))
    artifact = read_artifact(relative_path)
    expected_hash = _string(entry, "sha256")
    actual_hash = hashlib.sha256(artifact).hexdigest()
    _require_equal(actual_hash, expected_hash, f"artifact SHA-256 for {relative_path}")
    return artifact


def _safe_relative_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise SnapshotArtifactError("Artifact paths must be normalized relative POSIX paths.")
    return Path(*path.parts)


def _calendar_from_json(snapshot: DatasetSnapshot, content: bytes) -> InMemorySnapshotCalendar:
    payload = _json_object(content, "calendar artifact")
    provenance_payload = _object(payload, "provenance")
    provenance = CalendarProvenance(
        _string(provenance_payload, "source"),
        _string(provenance_payload, "version"),
        _string(provenance_payload, "contentHash"),
    )
    session_records = _array(payload, "sessions")
    sessions = {_asset_session(record): _calendar_session(record) for record in session_records}
    if len(sessions) != len(session_records):
        raise SnapshotArtifactError("Calendar artifact contains duplicate AssetSessions.")
    try:
        if "components" in payload:
            components = tuple(_calendar_component(item) for item in _array(payload, "components"))
            validate_composite_calendar(provenance, components, sessions)
        return InMemorySnapshotCalendar(snapshot.snapshot_id, provenance, sessions)
    except ValueError as error:
        raise SnapshotArtifactError("Calendar artifact is invalid.") from error


def _calendar_component(payload: dict[str, object]) -> CalendarComponent:
    provenance_payload = _object(payload, "provenance")
    try:
        return CalendarComponent(
            AssetCalendar(_string(payload, "calendar")),
            tuple(_string_array(payload, "assets")),
            CalendarProvenance(
                _string(provenance_payload, "source"),
                _string(provenance_payload, "version"),
                _string(provenance_payload, "contentHash"),
            ),
        )
    except ValueError as error:
        raise SnapshotArtifactError("Calendar component is invalid.") from error


def _daily_bars_from_ndjson(
    snapshot_id: str, content: bytes, format_version: int
) -> InMemorySnapshotDailyBars:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise SnapshotArtifactError("Daily Bar artifact must be UTF-8.") from error
    bars = tuple(
        _daily_bar(_json_object(line.encode(), "Daily Bar record"), format_version)
        for line in lines
        if line
    )
    try:
        return InMemorySnapshotDailyBars(snapshot_id, bars)
    except ValueError as error:
        raise SnapshotArtifactError("Daily Bar artifact is invalid.") from error


def _reference_closes_from_ndjson(
    snapshot_id: str, content: bytes | None
) -> InMemorySnapshotReferenceCloses:
    if content is None:
        return InMemorySnapshotReferenceCloses(snapshot_id, ())
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise SnapshotArtifactError("Reference close artifact must be UTF-8.") from error
    try:
        closes = tuple(
            ReferenceClose(
                _string(record, "asset"),
                _date(record, "sessionDate"),
                _decimal(record, "close"),
            )
            for line in lines
            if line
            for record in (_json_object(line.encode(), "Reference close record"),)
        )
        return InMemorySnapshotReferenceCloses(snapshot_id, closes)
    except ValueError as error:
        raise SnapshotArtifactError("Reference close artifact is invalid.") from error


def _asset_session(record: dict[str, object]) -> AssetSession:
    return AssetSession(_string(record, "asset"), _date(record, "sessionDate"))


def _calendar_session(record: dict[str, object]) -> CalendarSession:
    try:
        state = CalendarSessionState(_string(record, "state"))
        completed_at = (
            _utc_datetime(record, "completedAt") if state is CalendarSessionState.EXPECTED else None
        )
        if state is CalendarSessionState.NON_TRADING and record.get("completedAt") is not None:
            raise SnapshotArtifactError("NON_TRADING sessions must not define completedAt.")
        return CalendarSession(state, completed_at)
    except ValueError as error:
        raise SnapshotArtifactError("Calendar session record is invalid.") from error


def _daily_bar(record: dict[str, object], format_version: int) -> DailyBar:
    return DailyBar(
        _string(record, "asset"),
        _date(record, "sessionDate"),
        _decimal(record, "open"),
        _decimal(record, "high"),
        _decimal(record, "low"),
        _decimal(record, "close"),
        Decimal(_integer(record, "volume")) if format_version == 1 else _decimal(record, "volume"),
    )


def _read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise SnapshotArtifactError(f"Snapshot artifact is unavailable: {path.name}") from error


def _json_object(content: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotArtifactError(f"{name} must be valid UTF-8 JSON.") from error
    if not isinstance(value, dict):
        raise SnapshotArtifactError(f"{name} must be a JSON object.")
    return cast(dict[str, object], value)


def _object(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise SnapshotArtifactError(f"{field} must be an object.")
    return cast(dict[str, object], value)


def _array(payload: dict[str, object], field: str) -> list[dict[str, object]]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise SnapshotArtifactError(f"{field} must be an array of objects.")
    values = cast(list[object], value)
    if any(not isinstance(item, dict) for item in values):
        raise SnapshotArtifactError(f"{field} must be an array of objects.")
    return [cast(dict[str, object], item) for item in values]


def _string_array(payload: dict[str, object], field: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise SnapshotArtifactError(f"{field} must be an array of strings.")
    values = cast(list[object], value)
    if any(not isinstance(item, str) for item in values):
        raise SnapshotArtifactError(f"{field} must be an array of strings.")
    return [cast(str, item) for item in values]


def _string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise SnapshotArtifactError(f"{field} must be a string.")
    return value


def _integer(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SnapshotArtifactError(f"{field} must be an integer.")
    return value


def _boolean(payload: dict[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise SnapshotArtifactError(f"{field} must be a boolean.")
    return value


def _date(payload: dict[str, object], field: str) -> date:
    try:
        return date.fromisoformat(_string(payload, field))
    except ValueError as error:
        raise SnapshotArtifactError(f"{field} must be an ISO date.") from error


def _utc_datetime(payload: dict[str, object], field: str) -> datetime:
    value = _string(payload, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SnapshotArtifactError(f"{field} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SnapshotArtifactError(f"{field} must be an exact UTC timestamp.")
    return parsed.astimezone(UTC)


def _decimal(payload: dict[str, object], field: str) -> Decimal:
    value = _string(payload, field)
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise SnapshotArtifactError(f"{field} must be a Decimal string.") from error


def _require_equal(actual: object, expected: object, description: str) -> None:
    if actual != expected:
        raise SnapshotArtifactError(f"Unexpected {description}.")
