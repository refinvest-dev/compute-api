"""Manual composition root for publishing one complete immutable MVP snapshot."""

import argparse
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from refinvest.app.config import Settings, get_settings
from refinvest.ingestion.calendars.crypto_utc import build_crypto_utc_calendar
from refinvest.ingestion.calendars.us_equity import generate_us_equity_calendar
from refinvest.ingestion.snapshot.artifacts import FilesystemSnapshotArtifactPublisher
from refinvest.ingestion.snapshot.mvp import (
    MvpSnapshotIngestionCoordinator,
    MvpSnapshotRequest,
)
from refinvest.ingestion.vendors.binance_btcusdt import BinanceBtcusdtDailyBarsClient
from refinvest.ingestion.vendors.cboe_vix import CboeVixHistoricalClient
from refinvest.ingestion.vendors.massive_etf import MassiveEtfDailyBarsClient
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository

MVP_SNAPSHOT_SOURCE = "massive-eod-us-stocks+cboe-vix-history+binance-spot"
MVP_ADJUSTMENT_POLICY = "split-adjusted-ohlcv-cash-dividends-unadjusted"
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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
        CboeVixHistoricalClient(),
        BinanceBtcusdtDailyBarsClient(),
        FilesystemSnapshotArtifactPublisher(),
        PostgresDatasetSnapshotRepository(settings.database_url),
    )


def run_snapshot_ingestion(
    arguments: Sequence[str],
    *,
    settings: Settings,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    coordinator_factory: Callable[[Settings], MvpSnapshotIngestionCoordinator] = (
        create_mvp_snapshot_ingestion_coordinator
    ),
) -> str:
    """Parse one explicit coverage request and publish only fully completed daily sessions."""
    parsed = _parser().parse_args(arguments)
    start = _date_argument(parsed.start, "--start")
    end = _date_argument(parsed.end, "--end")
    current = now()
    if current.tzinfo is not UTC:
        raise ValueError("Snapshot ingestion clock must be an exact UTC datetime.")
    if start > end:
        raise ValueError("Snapshot coverage start must not exceed end.")
    if end >= current.date():
        raise ValueError("Snapshot coverage end must precede the current UTC date.")
    if not _SNAPSHOT_ID.fullmatch(parsed.snapshot_id):
        raise ValueError("--snapshot-id must be a safe immutable identifier.")
    if not settings.snapshot_storage_root:
        raise ValueError("REFINVEST_SNAPSHOT_STORAGE_ROOT is required for snapshot ingestion.")
    overrides = _official_overrides(Path(parsed.us_equity_overrides))
    request = MvpSnapshotRequest(
        parsed.snapshot_id,
        current.astimezone(UTC),
        MVP_SNAPSHOT_SOURCE,
        {"firstDate": start.isoformat(), "lastDate": end.isoformat()},
        MVP_ADJUSTMENT_POLICY,
        str(Path(settings.snapshot_storage_root) / parsed.snapshot_id),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    published = coordinator_factory(settings).publish(request, start, end, overrides)
    return json.dumps(
        {"snapshotId": published.snapshot_id, "storagePath": published.storage_path},
        separators=(",", ":"),
        sort_keys=True,
    )


def snapshot_ingestion_main() -> None:
    """Console-script entrypoint; configuration secrets remain in the environment."""
    import sys

    print(run_snapshot_ingestion(sys.argv[1:], settings=get_settings()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish one immutable MVP market-data snapshot.")
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--start", required=True, help="UTC coverage start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="UTC coverage end date (YYYY-MM-DD)")
    parser.add_argument(
        "--us-equity-overrides",
        required=True,
        help="UTF-8 JSON object: date -> null or exact UTC completion timestamp",
    )
    return parser


def _date_argument(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date.") from error


def _official_overrides(path: Path) -> Mapping[date, datetime | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("--us-equity-overrides must be a readable UTF-8 JSON file.") from error
    if not isinstance(payload, dict):
        raise ValueError("--us-equity-overrides must be a JSON object.")
    values = cast(dict[object, object], payload)
    overrides: dict[date, datetime | None] = {}
    for raw_date, raw_completed_at in values.items():
        if not isinstance(raw_date, str):
            raise ValueError("US-equity override dates must be strings.")
        session_date = _date_argument(raw_date, "US-equity override date")
        if raw_completed_at is None:
            overrides[session_date] = None
            continue
        if not isinstance(raw_completed_at, str):
            raise ValueError("US-equity override completion must be a timestamp or null.")
        try:
            completed_at = datetime.fromisoformat(raw_completed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("US-equity override completion must be ISO-8601.") from error
        if completed_at.tzinfo is not UTC:
            raise ValueError("US-equity override completion must be an exact UTC timestamp.")
        overrides[session_date] = completed_at.astimezone(UTC)
    return overrides
