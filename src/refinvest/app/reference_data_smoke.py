"""Deployment smoke check for one immutable snapshot's reference-data read path."""

import argparse
import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Protocol

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import create_snapshot_execution_data_loader
from refinvest.application.assets import ASSET_REFERENCES, availability_for_snapshot
from refinvest.application.backtest_executor import SnapshotExecutionDataLoader
from refinvest.application.dataset_snapshots import DatasetSnapshot, DatasetSnapshotRepository
from refinvest.application.series import build_series
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository

_SERIES_SYMBOLS = ("QQQ", "BTCUSDT")


class DatasetSnapshotRepositoryFactory(Protocol):
    def __call__(self, database_url: str) -> DatasetSnapshotRepository: ...


def verify_reference_data(
    settings: Settings,
    dataset_snapshot_id: str | None = None,
    *,
    snapshot_repository_factory: DatasetSnapshotRepositoryFactory = (
        PostgresDatasetSnapshotRepository
    ),
    loader_factory: Callable[[Settings], SnapshotExecutionDataLoader] = (
        create_snapshot_execution_data_loader
    ),
) -> dict[str, object]:
    """Verify catalog availability and a cross-calendar series from one selected snapshot."""
    if not settings.database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for reference-data smoke checks.")
    snapshots = snapshot_repository_factory(settings.database_url)
    snapshot = snapshots.get(dataset_snapshot_id) if dataset_snapshot_id else snapshots.latest()
    if snapshot is None:
        raise ValueError("The requested DatasetSnapshot is unavailable.")

    loader = loader_factory(settings)
    availability = tuple(
        availability_for_snapshot(asset, snapshot, loader) for asset in ASSET_REFERENCES
    )
    if any(item is None for item in availability):
        raise ValueError(
            "The selected DatasetSnapshot lacks a supported asset's calendar coverage."
        )

    series_start, series_end = _recent_cross_calendar_window(snapshot, loader)
    series = build_series(snapshot, loader, _SERIES_SYMBOLS, "PRICE", series_start, series_end)
    if any(not series[symbol] for symbol in _SERIES_SYMBOLS):
        raise ValueError("The selected DatasetSnapshot lacks recent cross-calendar series points.")
    return {
        "status": "ok",
        "datasetSnapshotId": snapshot.snapshot_id,
        "snapshotCreatedAt": snapshot.created_at,
        "adjustmentPolicy": snapshot.adjustment_policy,
        "availabilitySymbols": [asset.symbol for asset in ASSET_REFERENCES],
        "seriesSymbols": list(_SERIES_SYMBOLS),
        "seriesStart": series_start,
        "seriesEnd": series_end,
    }


def reference_data_smoke_main() -> None:
    """Console-script entrypoint without traceback or credential leakage."""
    parsed = _parser().parse_args()
    try:
        result = verify_reference_data(get_settings(), parsed.dataset_snapshot_id)
    except Exception:
        raise SystemExit(1) from None
    print(json.dumps(result, separators=(",", ":"), sort_keys=True, default=str))


def _recent_cross_calendar_window(
    snapshot: DatasetSnapshot, loader: SnapshotExecutionDataLoader
) -> tuple[date, date]:
    calendar = loader.load(snapshot).calendar
    latest_dates = tuple(
        _latest_expected_session(calendar.expected_sessions(symbol), symbol)
        for symbol in _SERIES_SYMBOLS
    )
    end = min(latest_dates)
    return end - timedelta(days=7), end


def _latest_expected_session(sessions: tuple[date, ...], symbol: str) -> date:
    if not sessions:
        raise ValueError(f"The selected DatasetSnapshot has no expected sessions for {symbol}.")
    return sessions[-1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify one immutable reference-data snapshot.")
    parser.add_argument(
        "--dataset-snapshot-id",
        help="Verify this immutable snapshot instead of the latest published snapshot.",
    )
    return parser
