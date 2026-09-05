"""Publish the deterministic local snapshot used by cross-service integration checks."""

from datetime import UTC, date, datetime, time
from decimal import Decimal

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import create_snapshot_artifact_publisher, snapshot_storage_path
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.workflow import publish_dataset_snapshot
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository

INTEGRATION_SNAPSHOT_ID = "integration-e2e-v1"
_CREATED_AT = datetime(2025, 1, 1, tzinfo=UTC)
_CALENDAR_PROVENANCE = CalendarProvenance("integration-fixture", "v1", "qqq-202501-v1")
_SESSIONS = tuple(date(2025, 1, day) for day in (2, 3, 6, 7, 8))


def publish_integration_fixture(settings: Settings) -> DatasetSnapshot:
    """Publish one immutable QQQ fixture, or return its already-published metadata."""
    if not settings.database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for integration fixture publication.")
    storage_path = snapshot_storage_path(settings, INTEGRATION_SNAPSHOT_ID)
    metadata = PostgresDatasetSnapshotRepository(settings.database_url)
    existing = metadata.get(INTEGRATION_SNAPSHOT_ID)
    if existing is not None:
        if existing.storage_path != storage_path:
            raise ValueError(
                "Integration fixture storage path does not match the published snapshot."
            )
        return existing

    snapshot = DatasetSnapshot(
        INTEGRATION_SNAPSHOT_ID,
        _CREATED_AT,
        "integration-fixture",
        {"firstDate": "2025-01-02", "lastDate": "2025-01-08"},
        "split-adjusted-ohlcv-cash-dividends-unadjusted",
        storage_path,
        _CALENDAR_PROVENANCE.source,
        _CALENDAR_PROVENANCE.version,
        _CALENDAR_PROVENANCE.content_hash,
    )
    publish_dataset_snapshot(
        snapshot,
        _CALENDAR_PROVENANCE,
        _sessions(),
        _bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
        artifacts=create_snapshot_artifact_publisher(settings),
        metadata=metadata,
    )
    return snapshot


def integration_fixture_main() -> None:
    """Console entrypoint; no market-data vendor credential is required."""
    try:
        snapshot = publish_integration_fixture(get_settings())
    except Exception:
        raise SystemExit(1) from None
    print(snapshot.snapshot_id)


def _sessions() -> dict[AssetSession, CalendarSession]:
    return {
        AssetSession("QQQ", session): CalendarSession(
            CalendarSessionState.EXPECTED,
            datetime.combine(session, time(21), UTC),
        )
        for session in _SESSIONS
    }


def _bars() -> tuple[DailyBar, ...]:
    prices = tuple(map(Decimal, ("100", "101", "102", "103", "104")))
    return tuple(
        DailyBar("QQQ", session, price, price, price, price, Decimal("1000"))
        for session, price in zip(_SESSIONS, prices, strict=True)
    )
