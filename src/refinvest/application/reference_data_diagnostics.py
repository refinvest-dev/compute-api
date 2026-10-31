"""Read-only catalog and cross-calendar diagnostics for one immutable snapshot."""

from collections.abc import Callable
from datetime import date, timedelta

from refinvest.application.assets import ASSET_REFERENCES, availability_for_snapshot
from refinvest.application.dataset_snapshots import DatasetSnapshot, DatasetSnapshotRepository
from refinvest.application.series import build_series
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader

_SERIES_SYMBOLS = ("QQQ", "BTCUSDT")


def verify_reference_data(
    snapshots: DatasetSnapshotRepository,
    dataset_snapshot_id: str | None = None,
    *,
    loader_factory: Callable[[], SnapshotExecutionDataLoader],
) -> dict[str, object]:
    """Verify catalog availability and a cross-calendar series from one selected snapshot."""
    snapshot = snapshots.get(dataset_snapshot_id) if dataset_snapshot_id else snapshots.latest()
    if snapshot is None:
        raise ValueError("The requested DatasetSnapshot is unavailable.")

    loader = loader_factory()
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
