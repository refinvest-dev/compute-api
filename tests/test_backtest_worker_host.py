from datetime import UTC, date, datetime, timedelta
from threading import Event
from time import sleep

import pytest

from refinvest.application.backtest_worker_host import (
    BacktestWorkerHost,
    CompletedBacktestExecution,
    RetryableBacktestFailure,
    TerminalBacktestFailure,
)
from refinvest.application.backtest_workers import ClaimedBacktestJob
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.engine.validation.models import Period


class FakeSnapshots:
    def get(self, snapshot_id: str) -> DatasetSnapshot | None:
        if snapshot_id != "snapshot-1":
            return None
        return DatasetSnapshot(
            "snapshot-1", datetime(2025, 1, 1, tzinfo=UTC), "vendor", {}, "adjusted", "path"
        )

    def latest(self) -> DatasetSnapshot | None:
        return None


class FakeJobs:
    def __init__(self, claims: list[ClaimedBacktestJob]) -> None:
        self.claims = claims
        self.heartbeats = 0
        self.failed: list[tuple[str, str, str, str | None]] = []
        self.retries: list[tuple[str, str, timedelta, str]] = []
        self.completed: list[tuple[str, str, dict[str, object], Period, str]] = []

    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> ClaimedBacktestJob | None:
        return self.claims.pop(0) if self.claims else None

    def heartbeat(self, run_id: str, worker_id: str, lease_duration: timedelta) -> bool:
        self.heartbeats += 1
        return True

    def mark_failed(
        self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
    ) -> bool:
        self.failed.append((run_id, worker_id, failure_reason, error_code))
        return True

    def mark_completed(
        self, run_id: str, worker_id: str, result_payload: dict[str, object], actual_period: Period,
        engine_version: str,
    ) -> bool:
        self.completed.append((run_id, worker_id, result_payload, actual_period, engine_version))
        return True

    def schedule_retry(
        self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
    ) -> bool:
        self.retries.append((run_id, worker_id, retry_delay, failure_reason))
        return True


class FakeExecutor:
    def __init__(
        self,
        outcome: CompletedBacktestExecution | TerminalBacktestFailure | RetryableBacktestFailure,
    ) -> None:
        self.outcome = outcome
        self.executed: list[str] = []

    def execute(
        self, job: object,
    ) -> CompletedBacktestExecution | TerminalBacktestFailure | RetryableBacktestFailure:
        self.executed.append(job.claim.run_id)  # type: ignore[attr-defined]
        return self.outcome


class WaitingExecutor(FakeExecutor):
    def execute(
        self, job: object,
    ) -> TerminalBacktestFailure | RetryableBacktestFailure:
        sleep(0.03)
        return super().execute(job)


class CrashingExecutor(FakeExecutor):
    def execute(
        self,
        job: object,
    ) -> TerminalBacktestFailure | RetryableBacktestFailure:
        raise RuntimeError("unexpected")


def claim(attempt_count: int = 1) -> ClaimedBacktestJob:
    return ClaimedBacktestJob("run-1", "worker-1", "snapshot-1", attempt_count, {})


def host(jobs: FakeJobs, executor: FakeExecutor) -> BacktestWorkerHost:
    return BacktestWorkerHost(
        "worker-1",
        timedelta(seconds=1),
        timedelta(milliseconds=5),
        jobs,
        FakeSnapshots(),
        executor,
    )


def test_terminal_failure_is_recorded_without_retry() -> None:
    jobs = FakeJobs([claim()])
    worker = host(jobs, FakeExecutor(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION")))
    assert worker.process_once()
    assert jobs.failed == [("run-1", "worker-1", "data corrupt", "DATASET_CORRUPTION")]
    assert jobs.retries == []


def test_successful_execution_is_persisted_by_lease_owner() -> None:
    jobs = FakeJobs([claim()])
    outcome = CompletedBacktestExecution(
        {"metrics": {}}, Period(date(2025, 1, 2), date(2025, 1, 3)), "v1"
    )
    assert host(jobs, FakeExecutor(outcome)).process_once()
    assert jobs.completed == [("run-1", "worker-1", {"metrics": {}}, outcome.actual_period, "v1")]


def test_retryable_failure_uses_the_existing_retry_policy() -> None:
    jobs = FakeJobs([claim(attempt_count=2)])
    worker = host(jobs, FakeExecutor(RetryableBacktestFailure("database unavailable")))
    assert worker.process_once()
    assert jobs.failed == []
    assert jobs.retries == [
        ("run-1", "worker-1", timedelta(minutes=2), "database unavailable")
    ]


def test_unexpected_executor_exception_uses_the_retry_policy() -> None:
    jobs = FakeJobs([claim()])
    worker = host(jobs, CrashingExecutor(RetryableBacktestFailure("unused")))
    assert worker.process_once()
    assert jobs.retries == [
        ("run-1", "worker-1", timedelta(seconds=30), "Backtest execution failed unexpectedly.")
    ]


def test_execution_heartbeat_renews_the_lease() -> None:
    jobs = FakeJobs([claim()])
    worker = host(
        jobs, WaitingExecutor(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    )
    assert worker.process_once()
    assert jobs.heartbeats >= 1


def test_no_job_does_not_invoke_executor() -> None:
    jobs = FakeJobs([])
    executor = FakeExecutor(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    assert not host(jobs, executor).process_once()
    assert executor.executed == []


def test_shutdown_stops_after_current_job_without_claiming_another() -> None:
    jobs = FakeJobs([claim(), claim()])
    stop_requested = Event()

    class ShutdownExecutor(FakeExecutor):
        def execute(
            self, job: object,
        ) -> TerminalBacktestFailure | RetryableBacktestFailure:
            stop_requested.set()
            return super().execute(job)

    worker = host(
        jobs, ShutdownExecutor(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION"))
    )
    worker.run_forever(stop_requested, timedelta(milliseconds=1))
    assert len(jobs.claims) == 1


def test_invalid_worker_intervals_are_rejected() -> None:
    with pytest.raises(ValueError, match="shorter"):
        BacktestWorkerHost(
            "worker-1",
            timedelta(seconds=1),
            timedelta(seconds=1),
            FakeJobs([]),
            FakeSnapshots(),
            FakeExecutor(TerminalBacktestFailure("data corrupt", "DATASET_CORRUPTION")),
        )
