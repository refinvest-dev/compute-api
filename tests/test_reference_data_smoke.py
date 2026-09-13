from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.app.config import Settings
from refinvest.app.reference_data_smoke import verify_reference_data
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionData
from refinvest.marketdata.domain import ClosePriceLookup, DailyBarAvailability


class FakeSnapshots:
    def __init__(self, snapshot: DatasetSnapshot | None) -> None:
        self.snapshot = snapshot

    def get(self, snapshot_id: str) -> DatasetSnapshot | None:
        return self.snapshot if self.snapshot and self.snapshot.snapshot_id == snapshot_id else None

    def latest(self) -> DatasetSnapshot | None:
        return self.snapshot


class FakeCalendar:
    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        if asset == "BTCUSDT":
            return (date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5))
        return (date(2025, 1, 2), date(2025, 1, 3))


class FakeMarketData:
    def get_close(self, asset: str, session_date: date) -> ClosePriceLookup:
        del asset, session_date
        return ClosePriceLookup(DailyBarAvailability.AVAILABLE, Decimal("100"))


class FakeLoader:
    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        return SnapshotExecutionData(
            snapshot.snapshot_id, FakeCalendar(), FakeMarketData(), True, True
        )


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot-1", datetime(2025, 1, 6, tzinfo=UTC), "test", {}, "adjusted", "path"
    )


def _settings() -> Settings:
    return Settings(None, database_url="postgresql://example")


def test_verifies_catalog_and_cross_calendar_series_from_one_snapshot() -> None:
    result = verify_reference_data(
        _settings(),
        snapshot_repository_factory=lambda _: FakeSnapshots(_snapshot()),
        loader_factory=lambda _: FakeLoader(),
    )

    assert result["status"] == "ok"
    assert result["datasetSnapshotId"] == "snapshot-1"
    assert result["availabilitySymbols"] == ["QQQ", "SPY", "TQQQ", "SOXL", "BTCUSDT"]
    assert result["seriesSymbols"] == ["QQQ", "BTCUSDT"]
    assert result["seriesStart"] == date(2024, 12, 27)
    assert result["seriesEnd"] == date(2025, 1, 3)


def test_rejects_an_unknown_requested_snapshot() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        verify_reference_data(
            _settings(),
            "missing",
            snapshot_repository_factory=lambda _: FakeSnapshots(_snapshot()),
            loader_factory=lambda _: FakeLoader(),
        )


def test_requires_a_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        verify_reference_data(Settings(None))
