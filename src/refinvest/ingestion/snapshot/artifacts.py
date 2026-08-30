"""Publish deterministic filesystem artifacts for one immutable DatasetSnapshot."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from shutil import move
from tempfile import mkdtemp

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.adapters.in_memory import (
    InMemorySnapshotCalendar,
    InMemorySnapshotDailyBars,
    SnapshotMarketData,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)


class SnapshotPublicationError(ValueError):
    """An immutable snapshot artifact cannot be validated or published safely."""


@dataclass(frozen=True)
class PublishedSnapshotArtifacts:
    snapshot_id: str
    storage_path: str
    calendar_sha256: str
    daily_bars_sha256: str


class FilesystemSnapshotArtifactPublisher:
    """Write a manifest-last, immutable filesystem artifact directory."""

    def publish(
        self,
        snapshot: DatasetSnapshot,
        provenance: CalendarProvenance,
        sessions: Mapping[AssetSession, CalendarSession],
        daily_bars: Iterable[DailyBar],
        *,
        corporate_actions_applied: bool,
        point_in_time_validation_passed: bool,
    ) -> PublishedSnapshotArtifacts:
        bars = tuple(daily_bars)
        _validate_snapshot_inputs(snapshot, provenance, sessions, bars)
        root = Path(snapshot.storage_path)
        if root.exists():
            raise SnapshotPublicationError("An immutable snapshot storage path already exists.")
        root.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(mkdtemp(prefix=f".{root.name}.", dir=root.parent))
        try:
            calendar_content = _calendar_content(provenance, sessions)
            daily_bars_content = _daily_bars_content(bars)
            _write_file(temporary_root / "calendar.json", calendar_content)
            _write_file(temporary_root / "daily-bars.ndjson", daily_bars_content)
            manifest_content = _manifest_content(
                snapshot.snapshot_id,
                calendar_content,
                daily_bars_content,
                corporate_actions_applied,
                point_in_time_validation_passed,
            )
            _write_file(temporary_root / "manifest.json", manifest_content)
            _publish_manifest_last(temporary_root, root)
        except OSError as error:
            raise SnapshotPublicationError("Snapshot artifact publication failed.") from error
        return PublishedSnapshotArtifacts(
            snapshot.snapshot_id,
            str(root),
            _sha256(calendar_content),
            _sha256(daily_bars_content),
        )


def _validate_snapshot_inputs(
    snapshot: DatasetSnapshot,
    provenance: CalendarProvenance,
    sessions: Mapping[AssetSession, CalendarSession],
    bars: tuple[DailyBar, ...],
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
        calendar = InMemorySnapshotCalendar(snapshot.snapshot_id, provenance, sessions)
        daily_bar_store = InMemorySnapshotDailyBars(snapshot.snapshot_id, bars)
        SnapshotMarketData(snapshot, calendar, daily_bar_store)
    except ValueError as error:
        raise SnapshotPublicationError(
            "Snapshot artifacts violate canonical invariants."
        ) from error


def _calendar_content(
    provenance: CalendarProvenance,
    sessions: Mapping[AssetSession, CalendarSession],
) -> bytes:
    records: list[dict[str, object]] = []
    for asset_session, session in sorted(
        sessions.items(), key=lambda item: (item[0].asset, item[0].session_date)
    ):
        record: dict[str, object] = {
            "asset": asset_session.asset,
            "sessionDate": asset_session.session_date.isoformat(),
            "state": session.state.value,
        }
        if session.state is CalendarSessionState.EXPECTED:
            assert session.completed_at is not None
            record["completedAt"] = session.completed_at.isoformat()
        records.append(record)
    return _json_bytes(
        {
            "provenance": {
                "source": provenance.source,
                "version": provenance.version,
                "contentHash": provenance.content_hash,
            },
            "sessions": records,
        }
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
            "volume": bar.volume,
        }
        for bar in sorted(bars, key=lambda item: (item.asset, item.session_date))
    )
    return b"".join(_json_bytes(record) + b"\n" for record in records)


def _manifest_content(
    snapshot_id: str,
    calendar_content: bytes,
    daily_bars_content: bytes,
    corporate_actions_applied: bool,
    point_in_time_validation_passed: bool,
) -> bytes:
    return _json_bytes(
        {
            "formatVersion": 1,
            "snapshotId": snapshot_id,
            "corporateActionsApplied": corporate_actions_applied,
            "pointInTimeValidationPassed": point_in_time_validation_passed,
            "calendar": {"path": "calendar.json", "sha256": _sha256(calendar_content)},
            "dailyBars": {"path": "daily-bars.ndjson", "sha256": _sha256(daily_bars_content)},
        }
    )


def _publish_manifest_last(temporary_root: Path, root: Path) -> None:
    root.mkdir()
    for name in ("calendar.json", "daily-bars.ndjson", "manifest.json"):
        move(str(temporary_root / name), root / name)
    temporary_root.rmdir()


def _write_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
