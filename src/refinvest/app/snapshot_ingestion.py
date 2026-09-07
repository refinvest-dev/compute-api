"""Manual composition root for publishing one complete immutable MVP snapshot."""

import argparse
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import cast
from uuid import uuid4

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import create_snapshot_artifact_publisher, snapshot_storage_path
from refinvest.ingestion.calendars.crypto_utc import build_crypto_utc_calendar
from refinvest.ingestion.calendars.us_equity import generate_us_equity_calendar
from refinvest.ingestion.snapshot.mvp import (
    MvpSnapshotIngestionCoordinator,
    MvpSnapshotRequest,
)
from refinvest.ingestion.vendors.binance_btcusdt import BinanceBtcusdtDailyBarsClient
from refinvest.ingestion.vendors.cboe_vix import CboeVixHistoricalClient
from refinvest.ingestion.vendors.massive_etf import MassiveEtfDailyBarsClient
from refinvest.persistence.postgres.binance_archive_revisions import (
    PostgresBinanceArchiveRevisionRepository,
)
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository

MVP_SNAPSHOT_SOURCE = "BINANCE_PUBLIC_DATA"
MVP_ADJUSTMENT_POLICY = "split-adjusted-ohlcv-cash-dividends-unadjusted"
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_LOGGER = logging.getLogger(__name__)


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
    """Parse one explicit coverage request and publish only fully completed daily sessions."""
    parsed = _parser().parse_args(arguments)
    current = now()
    if current.tzinfo is not UTC:
        raise ValueError("Snapshot ingestion clock must be an exact UTC datetime.")
    start = _date_argument(parsed.start, "--start") if parsed.start else _configured_start(settings)
    end = _date_argument(parsed.end, "--end") if parsed.end else current.date() - timedelta(days=1)
    if start > end:
        raise ValueError("Snapshot coverage start must not exceed end.")
    if end >= current.date():
        raise ValueError("Snapshot coverage end must precede the current UTC date.")
    snapshot_id = parsed.snapshot_id or _automatic_snapshot_id(end, current, snapshot_nonce())
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("--snapshot-id must be a safe immutable identifier.")
    override_path = parsed.us_equity_overrides or settings.us_equity_overrides_path
    if not override_path:
        raise ValueError("REFINVEST_US_EQUITY_OVERRIDES_PATH is required for snapshot ingestion.")
    overrides = _official_overrides(Path(override_path))
    request = MvpSnapshotRequest(
        snapshot_id,
        current.astimezone(UTC),
        MVP_SNAPSHOT_SOURCE,
        {"firstDate": start.isoformat(), "lastDate": end.isoformat()},
        MVP_ADJUSTMENT_POLICY,
        snapshot_storage_path(settings, snapshot_id),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    started = monotonic()
    _event(
        "snapshot_publication_started",
        snapshot_id=request.snapshot_id,
        coverage_start=start,
        coverage_end=end,
        storage_path=request.storage_path,
    )
    try:
        published = coordinator_factory(settings).publish(request, start, end, overrides)
    except Exception as error:
        _event(
            "snapshot_publication_failed",
            level=logging.WARNING,
            snapshot_id=request.snapshot_id,
            coverage_start=start,
            coverage_end=end,
            storage_path=request.storage_path,
            publication_duration_seconds=monotonic() - started,
            error_type=type(error).__name__,
        )
        raise
    _event(
        "snapshot_publication_completed",
        snapshot_id=published.snapshot_id,
        coverage_start=start,
        coverage_end=end,
        storage_path=published.storage_path,
        publication_duration_seconds=monotonic() - started,
    )
    return json.dumps(
        {"snapshotId": published.snapshot_id, "storagePath": published.storage_path},
        separators=(",", ":"),
        sort_keys=True,
    )


def snapshot_ingestion_main() -> None:
    """Console-script entrypoint; configuration secrets remain in the environment."""
    import sys

    try:
        result = run_snapshot_ingestion(sys.argv[1:], settings=get_settings())
    except Exception:
        raise SystemExit(1) from None
    print(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish one immutable MVP market-data snapshot.")
    parser.add_argument("--snapshot-id", help="Override the generated immutable snapshot ID")
    parser.add_argument("--start", help="UTC coverage start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="UTC coverage end date (YYYY-MM-DD)")
    parser.add_argument(
        "--us-equity-overrides",
        required=False,
        help="UTF-8 JSON object: date -> null or exact UTC completion timestamp",
    )
    return parser


def _configured_start(settings: Settings) -> date:
    if not settings.snapshot_start_date:
        raise ValueError("REFINVEST_SNAPSHOT_START_DATE is required for snapshot ingestion.")
    return _date_argument(settings.snapshot_start_date, "REFINVEST_SNAPSHOT_START_DATE")


def _automatic_snapshot_id(end: date, current: datetime, nonce: str) -> str:
    if not nonce or not re.fullmatch(r"[A-Za-z0-9]+", nonce):
        raise ValueError("Snapshot ID nonce must be alphanumeric.")
    return f"mvp-{end:%Y%m%d}-{current:%Y%m%dT%H%M%SZ}-{nonce}"


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


def _event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    _LOGGER.log(level, json.dumps({"event": name, **fields}, sort_keys=True, default=str))
