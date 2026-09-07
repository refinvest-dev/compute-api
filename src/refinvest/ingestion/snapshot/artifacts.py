"""Publish deterministic filesystem artifacts for one immutable DatasetSnapshot."""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import move
from tempfile import mkdtemp

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.calendar import (
    CalendarComponent,
    calendar_session_records,
    validate_composite_calendar,
)
from refinvest.marketdata.adapters.in_memory import (
    InMemorySnapshotCalendar,
    InMemorySnapshotDailyBars,
    InMemorySnapshotReferenceCloses,
    SnapshotMarketData,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    DailyBar,
    ReferenceClose,
)


class SnapshotPublicationError(ValueError):
    """An immutable snapshot artifact cannot be validated or published safely."""


@dataclass(frozen=True)
class PublishedSnapshotArtifacts:
    snapshot_id: str
    storage_path: str
    calendar_sha256: str
    daily_bars_sha256: str
    reference_closes_sha256: str | None = None


@dataclass(frozen=True)
class SnapshotArtifactContents:
    """Validated, deterministic bytes ready for an immutable storage adapter."""

    calendar: bytes
    daily_bars: bytes
    manifest: bytes
    reference_closes: bytes | None = None

    def objects_before_manifest(self) -> tuple[tuple[str, bytes], ...]:
        objects: list[tuple[str, bytes]] = [
            ("calendar.json", self.calendar),
            ("daily-bars.ndjson", self.daily_bars),
        ]
        if self.reference_closes is not None:
            objects.append(("reference-closes.ndjson", self.reference_closes))
        return tuple(objects)


class FilesystemSnapshotArtifactPublisher:
    """Write a manifest-last, immutable filesystem artifact directory."""

    def publish(
        self,
        snapshot: DatasetSnapshot,
        provenance: CalendarProvenance,
        sessions: Mapping[AssetSession, CalendarSession],
        daily_bars: Iterable[DailyBar],
        reference_closes: Iterable[ReferenceClose] = (),
        *,
        calendar_components: Sequence[CalendarComponent] = (),
        corporate_actions_applied: bool,
        point_in_time_validation_passed: bool,
    ) -> PublishedSnapshotArtifacts:
        contents = build_snapshot_artifact_contents(
            snapshot,
            provenance,
            sessions,
            daily_bars,
            reference_closes,
            calendar_components=calendar_components,
            corporate_actions_applied=corporate_actions_applied,
            point_in_time_validation_passed=point_in_time_validation_passed,
        )
        root = Path(snapshot.storage_path)
        if root.exists():
            raise SnapshotPublicationError("An immutable snapshot storage path already exists.")
        root.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(mkdtemp(prefix=f".{root.name}.", dir=root.parent))
        try:
            for name, content in contents.objects_before_manifest():
                _write_file(temporary_root / name, content)
            _write_file(temporary_root / "manifest.json", contents.manifest)
            _publish_manifest_last(temporary_root, root)
        except OSError as error:
            raise SnapshotPublicationError("Snapshot artifact publication failed.") from error
        return published_snapshot_artifacts(snapshot, contents)


def build_snapshot_artifact_contents(
    snapshot: DatasetSnapshot,
    provenance: CalendarProvenance,
    sessions: Mapping[AssetSession, CalendarSession],
    daily_bars: Iterable[DailyBar],
    reference_closes: Iterable[ReferenceClose] = (),
    *,
    calendar_components: Sequence[CalendarComponent] = (),
    corporate_actions_applied: bool,
    point_in_time_validation_passed: bool,
) -> SnapshotArtifactContents:
    """Validate canonical input and create the shared immutable artifact contract."""
    bars = tuple(daily_bars)
    closes = tuple(reference_closes)
    _validate_snapshot_inputs(snapshot, provenance, sessions, bars, closes, calendar_components)
    calendar = _calendar_content(provenance, sessions, calendar_components)
    bars_content = _daily_bars_content(bars)
    closes_content = _reference_closes_content(closes)
    return SnapshotArtifactContents(
        calendar,
        bars_content,
        _manifest_content(
            snapshot,
            calendar,
            bars_content,
            closes_content,
            corporate_actions_applied,
            point_in_time_validation_passed,
        ),
        closes_content,
    )


def published_snapshot_artifacts(
    snapshot: DatasetSnapshot, contents: SnapshotArtifactContents
) -> PublishedSnapshotArtifacts:
    return PublishedSnapshotArtifacts(
        snapshot.snapshot_id,
        snapshot.storage_path,
        _sha256(contents.calendar),
        _sha256(contents.daily_bars),
        _sha256(contents.reference_closes) if contents.reference_closes is not None else None,
    )


def _validate_snapshot_inputs(
    snapshot: DatasetSnapshot,
    provenance: CalendarProvenance,
    sessions: Mapping[AssetSession, CalendarSession],
    bars: tuple[DailyBar, ...],
    closes: tuple[ReferenceClose, ...],
    calendar_components: Sequence[CalendarComponent],
) -> None:
    if not snapshot.snapshot_id.strip():
        raise SnapshotPublicationError("DatasetSnapshot ID must not be empty.")
    if not snapshot.storage_path.strip():
        raise SnapshotPublicationError("DatasetSnapshot storage_path must not be empty.")
    if (
        snapshot.calendar_source,
        snapshot.calendar_version,
        snapshot.calendar_hash,
    ) != (provenance.source, provenance.version, provenance.content_hash):
        raise SnapshotPublicationError("Calendar provenance must match DatasetSnapshot metadata.")
    try:
        if calendar_components:
            validate_composite_calendar(provenance, calendar_components, sessions)
        calendar = InMemorySnapshotCalendar(snapshot.snapshot_id, provenance, sessions)
        daily_bar_store = InMemorySnapshotDailyBars(snapshot.snapshot_id, bars)
        reference_close_store = InMemorySnapshotReferenceCloses(snapshot.snapshot_id, closes)
        SnapshotMarketData(snapshot, calendar, daily_bar_store, reference_close_store)
    except ValueError as error:
        raise SnapshotPublicationError(
            "Snapshot artifacts violate canonical invariants."
        ) from error


def _calendar_content(
    provenance: CalendarProvenance,
    sessions: Mapping[AssetSession, CalendarSession],
    components: Sequence[CalendarComponent],
) -> bytes:
    payload: dict[str, object] = {
        "provenance": {
            "source": provenance.source,
            "version": provenance.version,
            "contentHash": provenance.content_hash,
        },
        "sessions": calendar_session_records(sessions),
    }
    if components:
        payload["components"] = _component_records(components)
    return _json_bytes(payload)


def _component_records(components: Sequence[CalendarComponent]) -> list[dict[str, object]]:
    return sorted(
        [
            {
                "calendar": component.calendar.value,
                "assets": sorted(component.assets),
                "provenance": {
                    "source": component.provenance.source,
                    "version": component.provenance.version,
                    "contentHash": component.provenance.content_hash,
                },
            }
            for component in components
        ],
        key=lambda item: str(item["calendar"]),
    )


def _daily_bars_content(bars: tuple[DailyBar, ...]) -> bytes:
    records = (
        {
            "asset": bar.asset,
            "sessionDate": bar.session_date.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
        }
        for bar in sorted(bars, key=lambda item: (item.asset, item.session_date))
    )
    return b"".join(_json_bytes(record) + b"\n" for record in records)


def _reference_closes_content(closes: tuple[ReferenceClose, ...]) -> bytes | None:
    if not closes:
        return None
    records = (
        {
            "asset": reference_close.asset,
            "sessionDate": reference_close.session_date.isoformat(),
            "close": str(reference_close.close),
        }
        for reference_close in sorted(closes, key=lambda item: (item.asset, item.session_date))
    )
    return b"".join(_json_bytes(record) + b"\n" for record in records)


def _manifest_content(
    snapshot: DatasetSnapshot,
    calendar_content: bytes,
    daily_bars_content: bytes,
    reference_closes_content: bytes | None,
    corporate_actions_applied: bool,
    point_in_time_validation_passed: bool,
) -> bytes:
    manifest: dict[str, object] = {
        "formatVersion": 3,
        "snapshotId": snapshot.snapshot_id,
        "corporateActionsApplied": corporate_actions_applied,
        "pointInTimeValidationPassed": point_in_time_validation_passed,
        "calendar": {"path": "calendar.json", "sha256": _sha256(calendar_content)},
        "dailyBars": {"path": "daily-bars.ndjson", "sha256": _sha256(daily_bars_content)},
        "sourceArtifacts": [
            {
                "uri": artifact.uri,
                "sha256": artifact.sha256,
                "retrievedAt": artifact.retrieved_at.isoformat().replace("+00:00", "Z"),
            }
            for artifact in sorted(
                snapshot.source_artifacts, key=lambda item: (item.uri, item.sha256)
            )
        ],
    }
    if reference_closes_content is not None:
        manifest["referenceCloses"] = {
            "path": "reference-closes.ndjson",
            "sha256": _sha256(reference_closes_content),
        }
    return _json_bytes(manifest)


def _publish_manifest_last(temporary_root: Path, root: Path) -> None:
    root.mkdir()
    for name in ("calendar.json", "daily-bars.ndjson", "reference-closes.ndjson", "manifest.json"):
        if not (temporary_root / name).exists():
            continue
        move(str(temporary_root / name), root / name)
    temporary_root.rmdir()


def _write_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
