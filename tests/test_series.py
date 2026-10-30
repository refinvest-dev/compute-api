from datetime import UTC, date, datetime
from decimal import Decimal
from json import loads

import refinvest.api.routes.series as series_route
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.series import SeriesPoint, build_series
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
    sessions = {
        "QQQ": (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)),
        "BTCUSDT": (date(2025, 1, 3), date(2025, 1, 4), date(2025, 1, 5)),
    }

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return self.sessions.get(asset, ())


class FakeMarketData:
    closes = {
        ("QQQ", date(2025, 1, 2)): Decimal("100"),
        ("QQQ", date(2025, 1, 3)): Decimal("110"),
        ("QQQ", date(2025, 1, 6)): Decimal("121"),
        ("BTCUSDT", date(2025, 1, 3)): Decimal("200"),
        ("BTCUSDT", date(2025, 1, 4)): Decimal("220"),
        ("BTCUSDT", date(2025, 1, 5)): Decimal("242"),
    }

    def get_close(self, asset: str, session_date: date) -> ClosePriceLookup:
        close = self.closes.get((asset, session_date))
        return ClosePriceLookup(
            (
                DailyBarAvailability.AVAILABLE
                if close is not None
                else DailyBarAvailability.MISSING_EXPECTED_SESSION
            ),
            close,
        )


class FakeLoader:
    def __init__(self, market_data: FakeMarketData | None = None) -> None:
        self.market_data = market_data or FakeMarketData()

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        return SnapshotExecutionData(
            snapshot.snapshot_id, FakeCalendar(), self.market_data, True, True
        )


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "snapshot-1", datetime(2025, 1, 7, tzinfo=UTC), "test", {}, "adjusted", "path"
    )


def test_series_preserves_each_assets_actual_sessions_and_normalizes() -> None:
    result = build_series(
        _snapshot(),
        FakeLoader(),
        ("QQQ", "BTCUSDT"),
        "NORMALIZED",
        date(2025, 1, 3),
        date(2025, 1, 5),
    )

    assert result["QQQ"] == (SeriesPoint(date(2025, 1, 3), Decimal("100")),)
    assert result["BTCUSDT"] == (
        SeriesPoint(date(2025, 1, 3), Decimal("100")),
        SeriesPoint(date(2025, 1, 4), Decimal("110")),
        SeriesPoint(date(2025, 1, 5), Decimal("121")),
    )


def test_return_uses_preceding_available_session_before_requested_period() -> None:
    result = build_series(
        _snapshot(), FakeLoader(), ("QQQ",), "RETURN", date(2025, 1, 3), date(2025, 1, 6)
    )

    assert result["QQQ"] == (
        SeriesPoint(date(2025, 1, 3), Decimal("0.1")),
        SeriesPoint(date(2025, 1, 6), Decimal("0.1")),
    )


def test_missing_expected_close_fails_fast_as_dataset_corruption(monkeypatch) -> None:
    market_data = FakeMarketData()
    market_data.closes.pop(("QQQ", date(2025, 1, 3)))

    response = series_route.get_series(
        "QQQ",
        "PRICE",
        date(2025, 1, 3),
        date(2025, 1, 3),
        FakeSnapshots(_snapshot()),
        FakeLoader(market_data),
        None,
    )

    assert response.status_code == 503
    assert loads(response.body)["errorCode"] == "DATASET_CORRUPTION"


def test_missing_snapshot_and_period_limits_have_explicit_responses() -> None:
    unavailable = series_route.get_series(
        "QQQ",
        "PRICE",
        date(2025, 1, 2),
        date(2025, 1, 3),
        FakeSnapshots(None),
        FakeLoader(),
        None,
    )
    unknown = series_route.get_series(
        "QQQ",
        "PRICE",
        date(2025, 1, 2),
        date(2025, 1, 3),
        FakeSnapshots(_snapshot()),
        FakeLoader(),
        "other",
    )
    too_long = series_route.get_series(
        "QQQ",
        "PRICE",
        date(2025, 1, 1),
        date(2035, 1, 2),
        FakeSnapshots(_snapshot()),
        FakeLoader(),
        None,
    )

    assert unavailable.status_code == 503
    assert loads(unavailable.body)["errorCode"] == "DATASET_UNAVAILABLE"
    assert unknown.status_code == 404
    assert too_long.status_code == 400


def test_series_rejects_vix_and_more_than_five_symbols(monkeypatch) -> None:
    vix = series_route.get_series(
        "VIX",
        "PRICE",
        date(2025, 1, 2),
        date(2025, 1, 3),
        FakeSnapshots(_snapshot()),
        FakeLoader(),
        None,
    )
    too_many = series_route.get_series(
        "QQQ,SPY,TQQQ,SOXL,BTCUSDT,IWM",
        "PRICE",
        date(2025, 1, 2),
        date(2025, 1, 3),
        FakeSnapshots(_snapshot()),
        FakeLoader(),
        None,
    )

    assert vix.status_code == 400
    assert loads(vix.body) == {"message": "Unsupported asset: VIX"}
    assert too_many.status_code == 400
    assert loads(too_many.body) == {"message": "Invalid series request."}
