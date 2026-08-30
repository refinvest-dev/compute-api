from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.engine.metrics import evaluate_simple_metric
from refinvest.engine.validation.models import AssetSymbol, MetricReference, MetricType
from refinvest.marketdata.adapters.in_memory import (
    CalendarResolutionError,
    InMemorySnapshotCalendar,
    InMemorySnapshotDailyBars,
    InMemorySnapshotReferenceCloses,
    SnapshotMarketData,
)
from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
    ReferenceClose,
    calendar_for_asset,
)
from refinvest.marketdata.ports import (
    MissingRequiredMarketDataError,
    require_close,
    require_daily_bar,
)


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
        snapshot_id,
        CalendarProvenance("exchange-calendar", "2025.1", "calendar-hash"),
        {
            asset_session: CalendarSession(
                state,
                datetime.combine(asset_session.session_date, time(21), UTC)
                if state is CalendarSessionState.EXPECTED
                else None,
            )
            for asset_session, state in states.items()
        },
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


def test_vix_close_only_reference_supports_metrics_without_fabricating_a_daily_bar() -> None:
    session = AssetSession("VIX", date(2025, 1, 2))
    market_data = SnapshotMarketData(
        snapshot("snapshot-1"),
        calendar({session: CalendarSessionState.EXPECTED}),
        InMemorySnapshotDailyBars("snapshot-1", []),
        InMemorySnapshotReferenceCloses(
            "snapshot-1", [ReferenceClose("VIX", session.session_date, Decimal("17.25"))]
        ),
    )

    assert require_close(market_data.get_close("VIX", session.session_date)) == Decimal("17.25")
    assert market_data.get_daily_bar("VIX", session.session_date).availability is (
        DailyBarAvailability.MISSING_EXPECTED_SESSION
    )
    assert evaluate_simple_metric(
        calendar({session: CalendarSessionState.EXPECTED}),
        market_data,
        AssetSymbol.VIX,
        session.session_date,
        MetricReference(AssetSymbol.VIX, MetricType.SIMPLE, None),
    ) == Decimal("17.25")


def test_vix_daily_bar_is_rejected() -> None:
    vix_bar = DailyBar(
        "VIX",
        date(2025, 1, 2),
        Decimal("17"),
        Decimal("17"),
        Decimal("17"),
        Decimal("17"),
        1,
    )

    with pytest.raises(ValueError, match="close-only"):
        SnapshotMarketData(
            snapshot("snapshot-1"),
            calendar({vix_bar.asset_session: CalendarSessionState.EXPECTED}),
            InMemorySnapshotDailyBars("snapshot-1", [vix_bar]),
        )


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
                {
                    bar().asset_session: CalendarSession(
                        CalendarSessionState.EXPECTED,
                        datetime(2025, 1, 2, 21, tzinfo=UTC),
                    )
                },
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


def test_expected_session_requires_an_immutable_utc_completion_time() -> None:
    with pytest.raises(ValueError, match="must include"):
        CalendarSession(CalendarSessionState.EXPECTED)
    with pytest.raises(ValueError, match="UTC"):
        CalendarSession(
            CalendarSessionState.EXPECTED,
            datetime(2025, 1, 2, 16, tzinfo=timezone(timedelta(hours=-5))),
        )


def test_non_trading_session_cannot_have_a_completion_time() -> None:
    with pytest.raises(ValueError, match="must not"):
        CalendarSession(
            CalendarSessionState.NON_TRADING,
            datetime(2025, 1, 4, 21, tzinfo=UTC),
        )


def test_calendar_exposes_expected_session_completion_times() -> None:
    sessions = {
        AssetSession("BTCUSDT", date(2025, 1, 3)): CalendarSession(
            CalendarSessionState.EXPECTED,
            datetime(2025, 1, 4, tzinfo=UTC),
        ),
        AssetSession("BTCUSDT", date(2025, 1, 4)): CalendarSession(
            CalendarSessionState.EXPECTED,
            datetime(2025, 1, 5, tzinfo=UTC),
        ),
    }
    completions = InMemorySnapshotCalendar(
        "snapshot-1", CalendarProvenance("exchange-calendar", "2025.1", "calendar-hash"), sessions
    ).expected_session_completions("BTCUSDT")
    assert [item.asset_session.session_date for item in completions] == [
        date(2025, 1, 3),
        date(2025, 1, 4),
    ]
    assert [item.completed_at for item in completions] == [
        datetime(2025, 1, 4, tzinfo=UTC),
        datetime(2025, 1, 5, tzinfo=UTC),
    ]


def test_calendar_rejects_non_increasing_completion_times() -> None:
    with pytest.raises(ValueError, match="strictly ordered"):
        InMemorySnapshotCalendar(
            "snapshot-1",
            CalendarProvenance("exchange-calendar", "2025.1", "calendar-hash"),
            {
                AssetSession("QQQ", date(2025, 1, 2)): CalendarSession(
                    CalendarSessionState.EXPECTED,
                    datetime(2025, 1, 2, 21, tzinfo=UTC),
                ),
                AssetSession("QQQ", date(2025, 1, 3)): CalendarSession(
                    CalendarSessionState.EXPECTED,
                    datetime(2025, 1, 2, 21, tzinfo=UTC),
                ),
            },
        )
