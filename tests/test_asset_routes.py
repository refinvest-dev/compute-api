from datetime import UTC, date, datetime
from json import loads

import refinvest.api.routes.assets as assets_route
from refinvest.app.config import Settings
from refinvest.application.backtest_executor import SnapshotExecutionData
from refinvest.application.dataset_snapshots import DatasetSnapshot


class FakeSnapshots:
    def __init__(self, snapshot: DatasetSnapshot | None) -> None:
        self.snapshot = snapshot

    def latest(self) -> DatasetSnapshot | None:
        return self.snapshot


class FakeCalendar:
    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return (date(2025, 1, 2), date(2025, 1, 3)) if asset == "QQQ" else ()


class FakeLoader:
    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        return SnapshotExecutionData(snapshot.snapshot_id, FakeCalendar(), object(), True, True)


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 4, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        "path",
    )


def test_list_assets_returns_static_catalog_without_a_snapshot() -> None:
    result = assets_route.list_assets(FakeSnapshots(None), Settings(None))

    assert [asset.symbol for asset in result.assets] == [
        "QQQ",
        "SPY",
        "TQQQ",
        "SOXL",
        "BTCUSDT",
    ]
    assert result.dataset_snapshot_id is None
    assert all(asset.execution_enabled for asset in result.assets)
    assert result.assets[0].data_availability is None


def test_availability_is_derived_from_one_selected_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        assets_route, "create_snapshot_execution_data_loader", lambda _: FakeLoader()
    )

    result = assets_route.get_asset_availability("QQQ", FakeSnapshots(_snapshot()), Settings(None))

    assert result.dataset_snapshot_id == "snapshot-1"
    assert result.data_availability.first_date == date(2025, 1, 2)
    assert result.data_availability.last_date == date(2025, 1, 3)


def test_supported_asset_without_snapshot_returns_dataset_unavailable() -> None:
    response = assets_route.get_asset_availability("QQQ", FakeSnapshots(None), Settings(None))

    assert response.status_code == 503
    assert loads(response.body) == {
        "message": "DatasetSnapshot is unavailable.",
        "errorCode": "DATASET_UNAVAILABLE",
    }


def test_vix_is_not_an_mvp_asset() -> None:
    response = assets_route.get_asset_availability("VIX", FakeSnapshots(None), Settings(None))

    assert response.status_code == 404
    assert loads(response.body) == {"message": "Asset not found."}
