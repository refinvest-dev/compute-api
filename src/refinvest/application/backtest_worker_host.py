"""Runtime host for leased Backtest jobs, without any Backtest engine dependency."""

from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Thread
from typing import Literal, Protocol

from refinvest.application.backtest_workers import (
    BacktestWorkerRepository,
    ExecutableBacktestJob,
    claim_executable_backtest,
    record_transient_failure,
)
from refinvest.application.dataset_snapshots import DatasetSnapshotRepository
from refinvest.engine.validation.models import Period

FatalErrorCode = Literal[
    "MISSING_REQUIRED_DATA",
    "DATASET_CORRUPTION",
    "CALENDAR_RESOLUTION_FAILED",
    "PRICE_DATA_MISSING",
]


@dataclass(frozen=True)
class TerminalBacktestFailure:
    failure_reason: str
    error_code: FatalErrorCode


@dataclass(frozen=True)
class RetryableBacktestFailure:
    failure_reason: str


@dataclass(frozen=True)
class CompletedBacktestExecution:
    result_payload: dict[str, object]
    actual_period: Period
    engine_version: str


BacktestExecutionOutcome = (
    CompletedBacktestExecution | TerminalBacktestFailure | RetryableBacktestFailure
)


class BacktestJobExecutor(Protocol):
    """Engine adapter that returns a canonical payload after successful execution."""

    def execute(self, job: ExecutableBacktestJob) -> BacktestExecutionOutcome: ...


class _LeaseHeartbeat:
    def __init__(
        self,
        jobs: BacktestWorkerRepository,
        run_id: str,
        worker_id: str,
        lease_duration: timedelta,
        interval: timedelta,
    ) -> None:
        self._jobs = jobs
        self._run_id = run_id
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._interval_seconds = interval.total_seconds()
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            if not self._jobs.heartbeat(self._run_id, self._worker_id, self._lease_duration):
                return


class BacktestWorkerHost:
    """Claims one job at a time and delegates execution through a future engine port."""

    def __init__(
        self,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        jobs: BacktestWorkerRepository,
        snapshots: DatasetSnapshotRepository,
        executor: BacktestJobExecutor,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        if heartbeat_interval <= timedelta(0) or heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat_interval must be positive and shorter than lease_duration.")
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._jobs = jobs
        self._snapshots = snapshots
        self._executor = executor

    def process_once(self) -> bool:
        """Process one claimed job; False means no eligible work was available."""
        job = claim_executable_backtest(
            self._worker_id, self._lease_duration, self._jobs, self._snapshots
        )
        if job is None:
            return False

        heartbeat = _LeaseHeartbeat(
            self._jobs,
            job.claim.run_id,
            self._worker_id,
            self._lease_duration,
            self._heartbeat_interval,
        )
        heartbeat.start()
        try:
            outcome = self._executor.execute(job)
        except Exception:
            record_transient_failure(
                job.claim, "Backtest execution failed unexpectedly.", self._jobs
            )
            return True
        finally:
            heartbeat.stop()

        if isinstance(outcome, CompletedBacktestExecution):
            self._jobs.mark_completed(
                job.claim.run_id,
                self._worker_id,
                outcome.result_payload,
                outcome.actual_period,
                outcome.engine_version,
            )
        elif isinstance(outcome, TerminalBacktestFailure):
            self._jobs.mark_failed(
                job.claim.run_id,
                self._worker_id,
                outcome.failure_reason,
                outcome.error_code,
            )
        else:
            record_transient_failure(job.claim, outcome.failure_reason, self._jobs)
        return True

    def run_forever(self, stop_requested: Event, poll_interval: timedelta) -> None:
        """Stop claiming on shutdown; any in-flight lease is recoverable after expiry."""
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive.")
        while not stop_requested.is_set():
            if not self.process_once():
                stop_requested.wait(poll_interval.total_seconds())
