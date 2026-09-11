"""Worker job lifecycle coordination, independent of FastAPI and PostgreSQL."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from refinvest.application.backtest_failure_observability import record_terminal_backtest_failure
from refinvest.application.dataset_snapshots import (
    DatasetSnapshot,
    DatasetSnapshotRepository,
)
from refinvest.engine.validation.models import Period

MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"
RETRY_DELAYS = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10))


@dataclass(frozen=True)
class ClaimedBacktestJob:
    """One exclusively leased job with its immutable snapshot already pinned."""

    run_id: str
    worker_id: str
    dataset_snapshot_id: str
    attempt_count: int
    request_payload: dict[str, object]
    created_at: datetime | None = None
    reclaimed_stale_lease: bool = False


@dataclass(frozen=True)
class ExecutableBacktestJob:
    """A claimed job with the immutable DatasetSnapshot selected for all attempts."""

    claim: ClaimedBacktestJob
    dataset_snapshot: DatasetSnapshot


class BacktestWorkerRepository(Protocol):
    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> ClaimedBacktestJob | None: ...

    def heartbeat(self, run_id: str, worker_id: str, lease_duration: timedelta) -> bool: ...

    def mark_failed(
        self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
    ) -> bool: ...

    def mark_completed(
        self,
        run_id: str,
        worker_id: str,
        result_payload: dict[str, object],
        actual_period: Period,
        engine_version: str,
    ) -> bool: ...

    def schedule_retry(
        self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
    ) -> bool: ...


def claim_executable_backtest(
    worker_id: str,
    lease_duration: timedelta,
    jobs: BacktestWorkerRepository,
    snapshots: DatasetSnapshotRepository,
) -> ExecutableBacktestJob | None:
    """Claim one job and load its already-pinned immutable snapshot."""
    claim = jobs.claim_next(worker_id, lease_duration)
    if claim is None:
        return None

    snapshot = snapshots.get(claim.dataset_snapshot_id)
    if snapshot is None:
        persisted = jobs.mark_failed(
            claim.run_id,
            worker_id,
            "The pinned DatasetSnapshot is unavailable.",
            MISSING_REQUIRED_DATA,
        )
        if persisted:
            record_terminal_backtest_failure(
                run_id=claim.run_id,
                snapshot_id=claim.dataset_snapshot_id,
                worker_id=worker_id,
                attempt=claim.attempt_count,
                error_code=MISSING_REQUIRED_DATA,
            )
        return None
    return ExecutableBacktestJob(claim, snapshot)


def retry_delay_for_attempt(attempt_count: int) -> timedelta | None:
    """Return the delay after a failed attempt, or None when retry budget is exhausted."""
    if attempt_count < 1 or attempt_count > len(RETRY_DELAYS):
        return None
    return RETRY_DELAYS[attempt_count - 1]


def record_transient_failure(
    claim: ClaimedBacktestJob, failure_reason: str, jobs: BacktestWorkerRepository
) -> bool:
    """Release a leased job for its bounded retry, or make the final failure terminal."""
    delay = retry_delay_for_attempt(claim.attempt_count)
    if delay is None:
        persisted = jobs.mark_failed(claim.run_id, claim.worker_id, failure_reason, None)
        if persisted:
            record_terminal_backtest_failure(
                run_id=claim.run_id,
                snapshot_id=claim.dataset_snapshot_id,
                worker_id=claim.worker_id,
                attempt=claim.attempt_count,
                error_code=None,
            )
        return persisted
    return jobs.schedule_retry(claim.run_id, claim.worker_id, delay, failure_reason)
