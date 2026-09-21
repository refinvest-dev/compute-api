"""Worker-owned metric adapter around committed durable repository operations."""

from datetime import UTC, datetime, timedelta

from refinvest.application.backtest_workers import BacktestWorkerRepository, ClaimedBacktestJob
from refinvest.engine.validation.models import Period
from refinvest.observability.metrics import WorkerMetrics, safe_record


class ObservedWorkerRepository:
    def __init__(self, delegate: BacktestWorkerRepository, metrics: WorkerMetrics) -> None:
        self._delegate = delegate
        self._metrics = metrics

    def claim_next(self, worker_id: str, lease_duration: timedelta) -> ClaimedBacktestJob | None:
        claim = self._delegate.claim_next(worker_id, lease_duration)
        if claim is not None:
            safe_record(self._metrics.claims.inc)
            if claim.created_at is not None:
                safe_record(
                    self._metrics.queue_wait,
                    max(0.0, (datetime.now(UTC) - claim.created_at).total_seconds()),
                )
        return claim

    def heartbeat(self, run_id: str, worker_id: str, lease_duration: timedelta) -> bool:
        return self._delegate.heartbeat(run_id, worker_id, lease_duration)

    def mark_failed(
        self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
    ) -> bool:
        persisted = self._delegate.mark_failed(run_id, worker_id, failure_reason, error_code)
        if persisted:
            safe_record(self._metrics.failed, error_code)
        return persisted

    def mark_completed(
        self,
        run_id: str,
        worker_id: str,
        result_payload: dict[str, object],
        actual_period: Period,
        engine_version: str,
    ) -> bool:
        persisted = self._delegate.mark_completed(
            run_id, worker_id, result_payload, actual_period, engine_version
        )
        if persisted:
            safe_record(self._metrics.terminal.labels("COMPLETED").inc)
        return persisted

    def schedule_retry(
        self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
    ) -> bool:
        persisted = self._delegate.schedule_retry(run_id, worker_id, retry_delay, failure_reason)
        if persisted:
            safe_record(self._metrics.retries.inc)
        return persisted
