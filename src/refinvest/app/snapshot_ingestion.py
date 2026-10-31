"""Manual composition root for publishing one complete immutable MVP snapshot."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import create_snapshot_artifact_publisher, snapshot_storage_path
from refinvest.ingestion.calendars.crypto_utc import build_crypto_utc_calendar
from refinvest.ingestion.calendars.us_equity import generate_us_equity_calendar
from refinvest.ingestion.snapshot import cli
from refinvest.ingestion.snapshot.mvp import MvpSnapshotIngestionCoordinator
from refinvest.ingestion.vendors.binance_btcusdt import BinanceBtcusdtDailyBarsClient
from refinvest.ingestion.vendors.massive_etf import MassiveEtfDailyBarsClient
from refinvest.persistence.postgres.binance_archive_revisions import (
    PostgresBinanceArchiveRevisionRepository,
)
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository


def create_mvp_snapshot_ingestion_coordinator(
    settings: Settings,
) -> MvpSnapshotIngestionCoordinator:
    """Compose the coordinator from deployment configuration and approved adapters."""
    if not settings.database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for snapshot ingestion.")
    if not settings.massive_api_key:
        raise ValueError("REFINVEST_MASSIVE_API_KEY is required for snapshot ingestion.")
    return MvpSnapshotIngestionCoordinator(
        generate_us_equity_calendar,
        build_crypto_utc_calendar,
        MassiveEtfDailyBarsClient(settings.massive_api_key),
        BinanceBtcusdtDailyBarsClient(
            revisions=PostgresBinanceArchiveRevisionRepository(settings.database_url)
        ),
        create_snapshot_artifact_publisher(settings),
        PostgresDatasetSnapshotRepository(settings.database_url),
    )


def run_snapshot_ingestion(
    arguments: Sequence[str],
    *,
    settings: Settings,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    snapshot_nonce: Callable[[], str] = lambda: uuid4().hex,
    coordinator_factory: Callable[[Settings], MvpSnapshotIngestionCoordinator] = (
        create_mvp_snapshot_ingestion_coordinator
    ),
) -> str:
    """Supply configuration and lazy adapters to the feature-owned CLI workflow."""
    return cli.run_snapshot_ingestion(
        arguments,
        configured_start=settings.snapshot_start_date,
        us_equity_overrides_path=settings.us_equity_overrides_path,
        storage_path=lambda snapshot_id: snapshot_storage_path(settings, snapshot_id),
        coordinator_factory=lambda: coordinator_factory(settings),
        now=now,
        snapshot_nonce=snapshot_nonce,
    )


def snapshot_ingestion_main() -> None:
    """Console-script entrypoint; configuration secrets remain in the environment."""
    import sys

    try:
        result = run_snapshot_ingestion(sys.argv[1:], settings=get_settings())
    except Exception:
        raise SystemExit(1) from None
    print(result)
