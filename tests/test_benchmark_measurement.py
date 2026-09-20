from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import cast

from refinvest.application.backtest_executor import DailyBarBacktestExecutor
from refinvest.application.backtest_worker_host import (
    CompletedBacktestExecution,
    TerminalBacktestFailure,
)
from refinvest.application.backtest_workers import ExecutableBacktestJob
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionData
from refinvest.benchmarking.inputs import FixtureInput, ScenarioInput
from refinvest.benchmarking.measurement import REQUIRED_STAGES, run_warm_benchmark
from refinvest.engine.validation.models import BacktestRequest, Period
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


class CountingLoader:
    def __init__(self, data: SnapshotExecutionData) -> None:
        self.data = data
        self.calls = 0

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        assert snapshot.snapshot_id == self.data.snapshot_id
        self.calls += 1
        return self.data


def test_warmups_are_excluded_and_all_production_stages_are_measured(tmp_path: Path) -> None:
    fixture, scenario, loader = _inputs(tmp_path)
    executor = DailyBarBacktestExecutor(loader, "engine-v1")

    result = run_warm_benchmark(fixture, scenario, executor, warmups=1, iterations=2)

    assert loader.calls == 3
    assert len(result.samples) == 2
    assert result.determinism_passed
    assert len({sample.result_hash for sample in result.samples}) == 1
    assert all(sample.status == "SUCCEEDED" for sample in result.samples)
    assert all(set(sample.stage_nanoseconds) == set(REQUIRED_STAGES) for sample in result.samples)
    assert all(sample.total_nanoseconds >= 0 for sample in result.samples)
    assert all(
        duration >= 0 for sample in result.samples for duration in sample.stage_nanoseconds.values()
    )
    assert all(sample.python_peak_allocated_bytes >= 0 for sample in result.samples)


def test_warmup_hash_mismatch_and_failed_iteration_are_not_hidden(tmp_path: Path) -> None:
    fixture, scenario, _ = _inputs(tmp_path)
    alternating = cast(DailyBarBacktestExecutor, AlternatingExecutor())
    mismatch = run_warm_benchmark(fixture, scenario, alternating, warmups=1, iterations=1)

    assert not mismatch.determinism_passed
    assert any(item["field"] == "resultHash" for item in mismatch.mismatches)

    failing = cast(DailyBarBacktestExecutor, FailingExecutor())
    failure = run_warm_benchmark(fixture, scenario, failing, warmups=0, iterations=1)
    assert len(failure.samples) == 1
    assert failure.samples[0].status == "FAILED"
    assert not failure.determinism_passed


def _inputs(tmp_path: Path) -> tuple[FixtureInput, ScenarioInput, CountingLoader]:
    snapshot = DatasetSnapshot(
        "performance-mvp-daily-v1",
        datetime(2025, 1, 1, tzinfo=UTC),
        "test",
        {"firstDate": "2025-01-02", "lastDate": "2025-01-03"},
        "adjusted",
        str(tmp_path),
        "test",
        "1",
        "hash",
    )
    sessions = (date(2025, 1, 2), date(2025, 1, 3))
    calendar = InMemorySnapshotCalendar(
        snapshot.snapshot_id,
        CalendarProvenance("test", "1", "hash"),
        {
            AssetSession("QQQ", session): CalendarSession(
                CalendarSessionState.EXPECTED, datetime.combine(session, time(21), UTC)
            )
            for session in sessions
        },
    )
    bars = InMemorySnapshotDailyBars(
        snapshot.snapshot_id,
        tuple(
            DailyBar(
                "QQQ",
                session,
                Decimal("100"),
                Decimal("100"),
                Decimal("100"),
                Decimal("100"),
                Decimal("1"),
            )
            for session in sessions
        ),
    )
    loader = CountingLoader(
        SnapshotExecutionData(
            snapshot.snapshot_id,
            calendar,
            SnapshotMarketData(snapshot, calendar, bars),
            True,
            True,
        )
    )
    fixture = FixtureInput(
        tmp_path / "fixture-manifest.json",
        tmp_path,
        snapshot.snapshot_id,
        1,
        "sha256:" + "0" * 64,
        snapshot,
        (),
    )
    request = {
        "strategyVersionId": "strategy-v1",
        "strategyVersion": {
            "primarySignalAsset": "QQQ",
            "conditions": [
                {
                    "operator": "LT",
                    "logicalCombinator": None,
                    "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": None},
                    "operandB": 0,
                }
            ],
            "executionAsset": "QQQ",
            "lag": 0,
            "exit": {"holdingSignalSessions": 1},
            "positionPolicy": {
                "longOnly": True,
                "singlePosition": True,
                "duplicateEntry": "IGNORE",
            },
        },
        "feeModel": {"commission": 0, "slippage": 0},
        "period": {"start": "2025-01-02", "end": "2025-01-03"},
        "datasetSnapshotId": snapshot.snapshot_id,
    }
    scenario = ScenarioInput(
        tmp_path / "scenario.json", "zero", 1, snapshot.snapshot_id, 1, request
    )
    return fixture, scenario, loader


class AlternatingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute_request(
        self, job: ExecutableBacktestJob, request: BacktestRequest, observer: object
    ) -> CompletedBacktestExecution:
        del request, observer
        self.calls += 1
        trade_count = self.calls
        return CompletedBacktestExecution(
            _result_payload(job.claim.run_id, trade_count),
            Period(date(2025, 1, 2), date(2025, 1, 3)),
            "engine-v1",
        )


class FailingExecutor:
    def execute_request(
        self, job: ExecutableBacktestJob, request: BacktestRequest, observer: object
    ) -> TerminalBacktestFailure:
        del job, request, observer
        return TerminalBacktestFailure("failed", "DATASET_CORRUPTION")


def _result_payload(run_id: str, trade_count: int) -> dict[str, object]:
    return {
        "backtestRunId": run_id,
        "metrics": {"tradeCount": trade_count},
        "equityCurve": [],
        "trades": [],
        "benchmark": {"primary": {"asset": "QQQ", "equityCurve": []}},
        "signalExecutionMarketRelation": "SAME_MARKET",
        "signalExecutionDelay": {"median": "0", "max": "0", "distribution": []},
        "sampleSizeWarning": "LOW",
        "dataIntegrityStatus": {
            "datasetSnapshotId": "performance-mvp-daily-v1",
            "corporateActionsApplied": True,
            "pointInTimeValidationPassed": True,
        },
    }
