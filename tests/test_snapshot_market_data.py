from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.adapters.in_memory import InMemorySnapshotDailyBars, SnapshotMarketData
from refinvest.marketdata.domain import (
    AssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)
from refinvest.marketdata.ports import MissingRequiredMarketDataError, require_daily_bar


def snapshot(snapshot_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id, datetime(2025, 1, 1, tzinfo=UTC), "test", {}, "adjusted", "path"
    )


def bar(close: str = "101") -> DailyBar:
    return DailyBar(
        "QQQ",
        date(2025, 1, 2),
        Decimal("100"),
        Decimal("102"),
        Decimal("99"),
        Decimal(close),
        1000,
    )


def test_snapshot_market_data_is_fixed_to_one_snapshot() -> None:
    data = InMemorySnapshotDailyBars("snapshot-1", [bar()])
    market_data = SnapshotMarketData(snapshot("snapshot-1"), data)
    lookup = market_data.get_daily_bar("QQQ", date(2025, 1, 2))
    assert lookup.availability is DailyBarAvailability.AVAILABLE
    assert require_daily_bar(lookup).close == Decimal("101")


def test_adapter_rejects_bars_from_a_different_snapshot() -> None:
    with pytest.raises(ValueError, match="resolved DatasetSnapshot"):
        SnapshotMarketData(snapshot("snapshot-1"), InMemorySnapshotDailyBars("snapshot-2", [bar()]))


def test_expected_session_data_is_not_silently_corrected() -> None:
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"), InMemorySnapshotDailyBars("snapshot-1", [])
    )
    lookup = market_data.get_daily_bar("QQQ", date(2025, 1, 2))
    assert lookup.availability is DailyBarAvailability.MISSING_EXPECTED_SESSION
    with pytest.raises(MissingRequiredMarketDataError):
        require_daily_bar(lookup)


def test_calendar_classified_non_trading_session_is_distinct_from_missing_data() -> None:
    session = AssetSession("QQQ", date(2025, 1, 4))
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"), InMemorySnapshotDailyBars("snapshot-1", [], [session])
    )
    lookup = market_data.get_daily_bar("QQQ", date(2025, 1, 4))
    assert lookup.availability is DailyBarAvailability.NON_TRADING_SESSION
    with pytest.raises(ValueError, match="non-trading"):
        require_daily_bar(lookup)


def test_duplicate_bars_are_rejected_to_preserve_snapshot_determinism() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        InMemorySnapshotDailyBars("snapshot-1", [bar(), bar()])


def test_lookup_requires_a_bar_only_for_available_state() -> None:
    with pytest.raises(ValueError, match="must contain"):
        DailyBarLookup(DailyBarAvailability.AVAILABLE)
