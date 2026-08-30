from datetime import UTC, date, datetime, time
from decimal import Decimal

from refinvest.application.backtest_executor import (
    DailyBarBacktestExecutor,
    SnapshotExecutionData,
)
from refinvest.application.backtest_worker_host import CompletedBacktestExecution
from refinvest.application.backtest_workers import ClaimedBacktestJob, ExecutableBacktestJob
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.marketdata.adapters.in_memory import (
    InMemorySnapshotCalendar,
    InMemorySnapshotDailyBars,
    SnapshotMarketData,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)


class SnapshotLoader:
    def __init__(self, data: SnapshotExecutionData) -> None:
        self.data = data
        self.loaded_snapshot_ids: list[str] = []

    def load(self, snapshot_id: str) -> SnapshotExecutionData:
        self.loaded_snapshot_ids.append(snapshot_id)
        return self.data


def test_executes_btc_weekend_signal_at_next_tqqq_session_deterministically() -> None:
    snapshot = DatasetSnapshot(
        "snapshot-1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {},
        "adjusted",
        "memory://snapshot-1",
        "test-calendar",
        "v1",
        "calendar-hash",
    )
    btc_sessions = tuple(date(2025, 1, day) for day in range(3, 8))
    tqqq_sessions = (date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7))
    calendar = InMemorySnapshotCalendar(
        snapshot.snapshot_id,
        CalendarProvenance("test-calendar", "v1", "calendar-hash"),
        {
            **{
                AssetSession("BTCUSDT", session): CalendarSession(
                    CalendarSessionState.EXPECTED,
                    datetime.combine(session, time(23, 59), UTC),
                )
                for session in btc_sessions
            },
            **{
                AssetSession("TQQQ", session): CalendarSession(
                    CalendarSessionState.EXPECTED,
                    datetime.combine(session, time(21), UTC),
                )
                for session in tqqq_sessions
            },
        },
    )
    daily_bars = InMemorySnapshotDailyBars(
        snapshot.snapshot_id,
        [
            *[
                _bar("BTCUSDT", session, "100" if session.day == 3 else "1")
                for session in btc_sessions
            ],
            _bar("TQQQ", date(2025, 1, 3), "90"),
            _bar("TQQQ", date(2025, 1, 6), "100"),
            _bar("TQQQ", date(2025, 1, 7), "110"),
        ],
    )
    market_data = SnapshotMarketData(snapshot, calendar, daily_bars)
    loader = SnapshotLoader(SnapshotExecutionData("snapshot-1", calendar, market_data, True, True))
    executor = DailyBarBacktestExecutor(loader, "engine-v1")
    job = ExecutableBacktestJob(
        ClaimedBacktestJob("run-1", "worker-1", snapshot.snapshot_id, 1, _payload()), snapshot
    )

    first = executor.execute(job)
    second = executor.execute(job)

    assert isinstance(first, CompletedBacktestExecution)
    assert first == second
    assert loader.loaded_snapshot_ids == ["snapshot-1", "snapshot-1"]
    assert first.actual_period.start == date(2025, 1, 3)
    assert first.actual_period.end == date(2025, 1, 7)
    assert first.result_payload["sampleSizeWarning"] == "LOW"
    assert first.result_payload["trades"] == [
        {
            "signalTime": "2025-01-03T23:59:00+00:00",
            "entryTime": "2025-01-06T21:00:00+00:00",
            "entryPrice": "100",
            "exitTime": "2025-01-07T21:00:00+00:00",
            "exitPrice": "110",
            "returnPct": "0.1",
            "holdingPeriod": 3,
        }
    ]


def _bar(asset: str, session: date, price: str) -> DailyBar:
    value = Decimal(price)
    return DailyBar(asset, session, value, value, value, value, 1)


def _payload() -> dict[str, object]:
    return {
        "strategyVersionId": "strategy-v1",
        "strategyVersion": {
            "primarySignalAsset": "BTCUSDT",
            "conditions": [
                {
                    "operator": "GT",
                    "logicalCombinator": None,
                    "operandA": {"asset": "BTCUSDT", "metric": "SIMPLE", "window": None},
                    "operandB": "99",
                }
            ],
            "executionAsset": "TQQQ",
            "lag": 0,
            "exit": {"holdingSignalSessions": 3},
            "positionPolicy": None,
        },
        "feeModel": {"commission": "0", "slippage": "0"},
        "period": {"start": "2025-01-03", "end": "2025-01-07"},
        "datasetSnapshotId": "snapshot-1",
    }
