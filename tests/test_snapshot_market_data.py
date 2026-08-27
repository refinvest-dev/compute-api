from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.adapters.in_memory import (
    CalendarResolutionError,
    InMemorySnapshotCalendar,
    InMemorySnapshotDailyBars,
    SnapshotMarketData,
)
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSessionState,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
    calendar_for_asset,
)
from refinvest.marketdata.ports import MissingRequiredMarketDataError, require_daily_bar


def snapshot(snapshot_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id,
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        "path",
        "exchange-calendar",
        "2025.1",
        "calendar-hash",
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


def calendar(
    states: dict[AssetSession, CalendarSessionState], snapshot_id: str = "snapshot-1"
) -> InMemorySnapshotCalendar:
    return InMemorySnapshotCalendar(
        snapshot_id, CalendarProvenance("exchange-calendar", "2025.1", "calendar-hash"), states
    )


def test_snapshot_market_data_is_fixed_to_one_snapshot() -> None:
    data = InMemorySnapshotDailyBars("snapshot-1", [bar()])
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"),
        calendar({bar().asset_session: CalendarSessionState.EXPECTED}),
        data,
    )
    lookup = market_data.get_daily_bar("QQQ", date(2025, 1, 2))
    assert lookup.availability is DailyBarAvailability.AVAILABLE
    assert require_daily_bar(lookup).close == Decimal("101")


def test_adapter_rejects_bars_from_a_different_snapshot() -> None:
    with pytest.raises(ValueError, match="resolved DatasetSnapshot"):
        SnapshotMarketData(
            snapshot("snapshot-1"),
            calendar({bar().asset_session: CalendarSessionState.EXPECTED}, "snapshot-2"),
            InMemorySnapshotDailyBars("snapshot-2", [bar()]),
        )


def test_expected_session_data_is_not_silently_corrected() -> None:
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"),
        calendar({AssetSession("QQQ", date(2025, 1, 2)): CalendarSessionState.EXPECTED}),
        InMemorySnapshotDailyBars("snapshot-1", []),
    )
    lookup = market_data.get_daily_bar("QQQ", date(2025, 1, 2))
    assert lookup.availability is DailyBarAvailability.MISSING_EXPECTED_SESSION
    with pytest.raises(MissingRequiredMarketDataError):
        require_daily_bar(lookup)


def test_calendar_classified_non_trading_session_is_distinct_from_missing_data() -> None:
    session = AssetSession("QQQ", date(2025, 1, 4))
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"),
        calendar({session: CalendarSessionState.NON_TRADING}),
        InMemorySnapshotDailyBars("snapshot-1", []),
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


def test_missing_calendar_classification_fails_without_deriving_a_schedule() -> None:
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"), calendar({}), InMemorySnapshotDailyBars("snapshot-1", [])
    )
    with pytest.raises(CalendarResolutionError, match="calendar classification"):
        market_data.get_daily_bar("QQQ", date(2025, 1, 2))


def test_daily_bar_on_non_trading_session_is_rejected() -> None:
    with pytest.raises(ValueError, match="expected AssetSession"):
        SnapshotMarketData(
            snapshot("snapshot-1"),
            calendar({bar().asset_session: CalendarSessionState.NON_TRADING}),
            InMemorySnapshotDailyBars("snapshot-1", [bar()]),
        )


def test_calendar_provenance_must_match_snapshot_metadata() -> None:
    with pytest.raises(ValueError, match="provenance"):
        SnapshotMarketData(
            snapshot("snapshot-1"),
            InMemorySnapshotCalendar(
                "snapshot-1",
                CalendarProvenance("exchange-calendar", "2025.2", "calendar-hash"),
                {bar().asset_session: CalendarSessionState.EXPECTED},
            ),
            InMemorySnapshotDailyBars("snapshot-1", [bar()]),
        )


@pytest.mark.parametrize(
    ("asset", "expected_calendar"),
    [
        ("QQQ", AssetCalendar.US_EQUITY),
        ("VIX", AssetCalendar.US_EQUITY),
        ("BTCUSDT", AssetCalendar.CRYPTO_UTC),
    ],
)
def test_mvp_asset_calendar_policy(asset: str, expected_calendar: AssetCalendar) -> None:
    assert calendar_for_asset(asset) is expected_calendar


def test_crypto_calendar_rejects_non_trading_sessions() -> None:
    with pytest.raises(ValueError, match="always be expected"):
        calendar({AssetSession("BTCUSDT", date(2025, 1, 4)): CalendarSessionState.NON_TRADING})
