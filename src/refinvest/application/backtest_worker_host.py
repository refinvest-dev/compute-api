"""Runtime host for leased Backtest jobs, without any Backtest engine dependency."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import monotonic
from typing import Literal, Protocol

from refinvest.application.backtest_workers import (
    BacktestWorkerRepository,
    ExecutableBacktestJob,
    claim_executable_backtest,
    record_transient_failure,
    retry_delay_for_attempt,
)
from refinvest.application.dataset_snapshots import DatasetSnapshotRepository
from refinvest.engine.validation.models import Period

FatalErrorCode = Literal[
    "MISSING_REQUIRED_DATA",
    "DATASET_CORRUPTION",
    "CALENDAR_RESOLUTION_FAILED",
    "PRICE_DATA_MISSING",
    "EXECUTION_TIMEOUT",
]

_LOGGER = logging.getLogger(__name__)
_QUEUE_WAIT_WARNING = timedelta(minutes=10)
_EXECUTION_WARNING = timedelta(minutes=5)


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
        queue_wait_warning: timedelta = _QUEUE_WAIT_WARNING,
        execution_warning: timedelta = _EXECUTION_WARNING,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        if heartbeat_interval <= timedelta(0) or heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat_interval must be positive and shorter than lease_duration.")
        if queue_wait_warning <= timedelta(0) or execution_warning <= timedelta(0):
            raise ValueError("Worker observability warning thresholds must be positive.")
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._jobs = jobs
        self._snapshots = snapshots
        self._executor = executor
        self._queue_wait_warning = queue_wait_warning
        self._execution_warning = execution_warning

    def process_once(self) -> bool:
        """Process one claimed job; False means no eligible work was available."""
        job = claim_executable_backtest(
            self._worker_id, self._lease_duration, self._jobs, self._snapshots
        )
        if job is None:
            return False

        queue_wait_seconds = _queue_wait_seconds(job)
        _event(
            "backtest_job_claimed",
            run_id=job.claim.run_id,
            worker_id=self._worker_id,
            snapshot_id=job.claim.dataset_snapshot_id,
            attempt=job.claim.attempt_count,
            queue_wait_seconds=queue_wait_seconds,
        )
        if job.claim.reclaimed_stale_lease:
            _event(
                "backtest_stale_lease_reclaimed",
                level=logging.WARNING,
                run_id=job.claim.run_id,
                worker_id=self._worker_id,
                snapshot_id=job.claim.dataset_snapshot_id,
                attempt=job.claim.attempt_count,
            )
        if (
            queue_wait_seconds is not None
            and queue_wait_seconds >= self._queue_wait_warning.total_seconds()
        ):
            _event(
                "backtest_queue_wait_threshold_exceeded",
                level=logging.WARNING,
                run_id=job.claim.run_id,
                worker_id=self._worker_id,
                snapshot_id=job.claim.dataset_snapshot_id,
                attempt=job.claim.attempt_count,
                queue_wait_seconds=queue_wait_seconds,
            )

        heartbeat = _LeaseHeartbeat(
            self._jobs,
            job.claim.run_id,
            self._worker_id,
            self._lease_duration,
            self._heartbeat_interval,
        )
        heartbeat.start()
        started = monotonic()
        try:
            outcome = self._executor.execute(job)
        except Exception:
            duration_seconds = monotonic() - started
            self._record_retryable_failure(
                job, "Backtest execution failed unexpectedly.", duration_seconds
            )
            return True
        finally:
            heartbeat.stop()

        duration_seconds = monotonic() - started
        if duration_seconds >= self._execution_warning.total_seconds():
            _event(
                "backtest_execution_duration_threshold_exceeded",
                level=logging.WARNING,
                run_id=job.claim.run_id,
                worker_id=self._worker_id,
                snapshot_id=job.claim.dataset_snapshot_id,
                attempt=job.claim.attempt_count,
                execution_duration_seconds=duration_seconds,
            )

        if isinstance(outcome, CompletedBacktestExecution):
            persisted = self._jobs.mark_completed(
                job.claim.run_id,
                self._worker_id,
                outcome.result_payload,
                outcome.actual_period,
                outcome.engine_version,
            )
            _event(
                "backtest_execution_completed",
                run_id=job.claim.run_id,
                worker_id=self._worker_id,
                snapshot_id=job.claim.dataset_snapshot_id,
                attempt=job.claim.attempt_count,
                execution_duration_seconds=duration_seconds,
                persisted=persisted,
            )
        elif isinstance(outcome, TerminalBacktestFailure):
            persisted = self._jobs.mark_failed(
                job.claim.run_id,
                self._worker_id,
                outcome.failure_reason,
                outcome.error_code,
            )
            _event(
                "backtest_execution_failed",
                level=logging.WARNING,
                run_id=job.claim.run_id,
                worker_id=self._worker_id,
                snapshot_id=job.claim.dataset_snapshot_id,
                attempt=job.claim.attempt_count,
                execution_duration_seconds=duration_seconds,
                error_code=outcome.error_code,
                persisted=persisted,
            )
        else:
            self._record_retryable_failure(job, outcome.failure_reason, duration_seconds)
        return True

    def _record_retryable_failure(
        self, job: ExecutableBacktestJob, failure_reason: str, duration_seconds: float
    ) -> None:
        retry_delay = retry_delay_for_attempt(job.claim.attempt_count)
        persisted = record_transient_failure(job.claim, failure_reason, self._jobs)
        _event(
            "backtest_retry_scheduled" if retry_delay is not None else "backtest_retry_exhausted",
            level=logging.WARNING,
            run_id=job.claim.run_id,
            worker_id=self._worker_id,
            snapshot_id=job.claim.dataset_snapshot_id,
            attempt=job.claim.attempt_count,
            execution_duration_seconds=duration_seconds,
            retry_delay_seconds=retry_delay.total_seconds() if retry_delay is not None else None,
            persisted=persisted,
        )

    def run_forever(self, stop_requested: Event, poll_interval: timedelta) -> None:
        """Stop claiming on shutdown; any in-flight lease is recoverable after expiry."""
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive.")
        while not stop_requested.is_set():
            if not self.process_once():
                stop_requested.wait(poll_interval.total_seconds())


def _queue_wait_seconds(job: ExecutableBacktestJob) -> float | None:
    if job.claim.created_at is None:
        return None
    return max(0, (datetime.now(UTC) - job.claim.created_at).total_seconds())


def _event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    _LOGGER.log(level, json.dumps({"event": name, **fields}, sort_keys=True, default=str))
