"""Publish the deterministic local snapshot used by cross-service integration checks."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import (
    create_snapshot_artifact_publisher,
    create_snapshot_execution_data_loader,
    snapshot_storage_path,
)
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
_CALENDAR_PROVENANCE = CalendarProvenance("integration-fixture", "v1", "mvp-5-assets-202501-v1")
_US_EQUITY_SESSIONS = tuple(date(2025, 1, day) for day in (2, 3, 6, 7, 8))
_CRYPTO_UTC_SESSIONS = tuple(date(2025, 1, day) for day in range(2, 9))
_ETF_ASSETS = ("QQQ", "SPY", "TQQQ", "SOXL")
_MVP_ASSETS = (*_ETF_ASSETS, "BTCUSDT")


def publish_integration_fixture(settings: Settings) -> DatasetSnapshot:
    """Publish one immutable five-asset MVP fixture, or return its published metadata."""
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
        _assert_existing_fixture_has_mvp_coverage(
            create_snapshot_execution_data_loader(settings)
            .load(existing)
            .calendar.expected_sessions
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
    us_equity = {
        AssetSession(asset, session): CalendarSession(
            CalendarSessionState.EXPECTED, datetime.combine(session, time(21), UTC)
        )
        for asset in _ETF_ASSETS
        for session in _US_EQUITY_SESSIONS
    }
    crypto = {
        AssetSession("BTCUSDT", session): CalendarSession(
            CalendarSessionState.EXPECTED, datetime.combine(session, time.min, UTC)
        )
        for session in _CRYPTO_UTC_SESSIONS
    }
    return {**us_equity, **crypto}


def _bars() -> tuple[DailyBar, ...]:
    etf_prices = tuple(map(Decimal, ("100", "101", "102", "103", "104")))
    crypto_prices = tuple(
        map(Decimal, ("90000", "91000", "92000", "93000", "94000", "95000", "96000"))
    )
    return (
        *(
            DailyBar(asset, session, price, price, price, price, Decimal("1000"))
            for asset in _ETF_ASSETS
            for session, price in zip(_US_EQUITY_SESSIONS, etf_prices, strict=True)
        ),
        *(
            DailyBar("BTCUSDT", session, price, price, price, price, Decimal("10"))
            for session, price in zip(_CRYPTO_UTC_SESSIONS, crypto_prices, strict=True)
        ),
    )


def _assert_existing_fixture_has_mvp_coverage(
    expected_sessions: Callable[[str], tuple[date, ...]],
) -> None:
    missing_assets = tuple(asset for asset in _MVP_ASSETS if not expected_sessions(asset))
    if missing_assets:
        raise ValueError(
            "Integration fixture is outdated and lacks MVP asset coverage; "
            "recreate the isolated integration database."
        )
