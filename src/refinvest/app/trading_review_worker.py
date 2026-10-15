"""Runnable, lease-bound Trading Review normalization worker."""

from __future__ import annotations

import logging
import signal
from datetime import UTC, datetime, timedelta
from os import getpid
from socket import gethostname
from threading import Event
from time import monotonic

from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.app.trading_review_normalization import (
    CompletedNormalization,
    ProcessIsolatedNormalizationExecutor,
)
from refinvest.application.trading_review_jobs import SafeTradingReviewFailure
from refinvest.observability.metrics import TradingReviewWorkerMetrics, safe_record, serve_metrics
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository

_LOG = logging.getLogger(__name__)


class TradingReviewWorkerHost:
    def __init__(
        self,
        repository: PostgresTradingReviewJobRepository,
        executor: ProcessIsolatedNormalizationExecutor,
        *,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        retention: timedelta,
        max_attempts: int = 3,
        metrics: TradingReviewWorkerMetrics | None = None,
    ) -> None:
        if heartbeat_interval <= timedelta(0) or heartbeat_interval >= lease_duration:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease.")
        self.repository = repository
        self.executor = executor
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.heartbeat_interval = heartbeat_interval
        self.retention = retention
        if max_attempts <= 0:
            raise ValueError("Attempt budget must be positive.")
        self.max_attempts = max_attempts
        self.metrics = metrics

    def run_once(self) -> bool:
        claim = self.repository.claim_next(self.worker_id, self.lease_duration)
        if claim is None:
            return False
        if self.metrics is not None:
            safe_record(self.metrics.claimed)
            safe_record(
                self.metrics.queue_wait,
                max(0.0, (datetime.now(UTC) - claim.created_at).total_seconds()),
            )
        began = monotonic()
        heartbeat_due = began + self.heartbeat_interval.total_seconds()

        def should_stop() -> bool:
            nonlocal heartbeat_due
            if self.repository.cancellation_requested(
                claim.job_id, claim.lease_owner, claim.attempt_token, claim.deletion_generation
            ):
                return True
            if monotonic() >= heartbeat_due:
                if not self.repository.heartbeat(
                    claim.job_id,
                    claim.lease_owner,
                    claim.attempt_token,
                    claim.deletion_generation,
                    self.lease_duration,
                ):
                    return True
                heartbeat_due = monotonic() + self.heartbeat_interval.total_seconds()
            return False

        try:
            outcome = self.executor.execute(claim, should_stop)
            if isinstance(outcome, CompletedNormalization):
                committed = self.repository.publish_completed(
                    claim,
                    payload=outcome.payload,
                    media_type=outcome.media_type,
                    payload_sha256=outcome.payload_sha256,
                    input_set_hash=outcome.input_set_hash,
                    reconciliation_manifest_hash=outcome.reconciliation_manifest_hash,
                    result_hash=outcome.result_hash,
                    retention=self.retention,
                )
                if committed and self.metrics is not None:
                    safe_record(self.metrics.terminal, "COMPLETED", monotonic() - began)
            elif outcome.failure.code == "JOB_CANCELLED":
                committed = self.repository.mark_cancelled(claim)
                if committed and self.metrics is not None:
                    safe_record(self.metrics.terminal, "CANCELLED", monotonic() - began)
            else:
                grant_consumed_or_unavailable = (
                    outcome.failure.stage == "ARTIFACT_FETCH"
                    and outcome.failure.code
                    in {
                        "ARTIFACT_REFERENCE_EXPIRED",
                        "ARTIFACT_ACCESS_DENIED",
                    }
                )
                delay = (
                    timedelta(seconds=1)
                    if (outcome.retryable or grant_consumed_or_unavailable)
                    and claim.attempt_number < self.max_attempts
                    else None
                )
                if delay is not None:
                    committed = self.repository.schedule_retry(claim, delay, outcome.failure)
                    if committed and self.metrics is not None:
                        safe_record(self.metrics.retry)
                else:
                    committed = self.repository.mark_failed(claim, outcome.failure)
                    if committed and self.metrics is not None:
                        safe_record(self.metrics.terminal, "FAILED", monotonic() - began)
                        safe_record(self.metrics.failure, outcome.failure.code)
        except Exception:
            _LOG.warning("trading_review_worker_attempt_failed")
            failure = SafeTradingReviewFailure(
                "TEMPORARY_DEPENDENCY_FAILURE",
                "Normalization worker could not finish its attempt.",
                True,
                "EXECUTION",
            )
            delay = timedelta(seconds=1) if claim.attempt_number < self.max_attempts else None
            if delay is not None:
                self.repository.schedule_retry(claim, delay, failure)
            else:
                self.repository.mark_failed(claim, failure)
        return True

    def run_forever(self, stop: Event, poll_interval: timedelta) -> None:
        while not stop.is_set():
            if not self.run_once():
                stop.wait(poll_interval.total_seconds())


def create_trading_review_worker(
    settings: Settings, metrics: TradingReviewWorkerMetrics | None = None
) -> TradingReviewWorkerHost:
    if settings.database_url is None:
        raise ValueError("REFINVEST_DATABASE_URL is required.")
    if settings.trading_review_active_job_limit is None:
        raise ValueError("REFINVEST_TRADING_REVIEW_ACTIVE_JOB_LIMIT is required.")
    if not settings.trading_review_timezone_database_version:
        raise ValueError("REFINVEST_TRADING_REVIEW_TZDB_VERSION is required.")
    if not settings.trading_review_identity_key or len(settings.trading_review_identity_key) < 32:
        raise ValueError("REFINVEST_TRADING_IDENTITY_KEY must have at least 32 characters.")
    if not settings.trading_review_masking_key or len(settings.trading_review_masking_key) < 32:
        raise ValueError("REFINVEST_TRADING_MASKING_KEY must have at least 32 characters.")
    if not settings.trading_review_artifact_hosts:
        raise ValueError("REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS is required.")
    payload_key = trading_review_payload_key(settings)
    return TradingReviewWorkerHost(
        PostgresTradingReviewJobRepository(
            settings.database_url,
            payload_key,
            active_job_limit=settings.trading_review_active_job_limit,
            grant_renewal_cutoff=timedelta(
                seconds=settings.trading_review_grant_renewal_cutoff_seconds
            ),
            grant_refresh_wait=timedelta(
                seconds=settings.trading_review_grant_refresh_wait_seconds
            ),
            max_attempts=settings.trading_review_max_attempts,
        ),
        ProcessIsolatedNormalizationExecutor(
            settings, timedelta(seconds=settings.trading_review_execution_timeout_seconds)
        ),
        worker_id=settings.worker_id or f"{gethostname()}-{getpid()}",
        lease_duration=timedelta(seconds=settings.worker_lease_seconds),
        heartbeat_interval=timedelta(seconds=settings.worker_heartbeat_seconds),
        retention=timedelta(hours=24),
        max_attempts=settings.trading_review_max_attempts,
        metrics=metrics,
    )


def trading_review_worker_main() -> None:
    stop = Event()

    def request_stop(_: int, __: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    settings = get_settings()
    metrics = TradingReviewWorkerMetrics() if settings.worker_metrics.enabled else None
    worker = create_trading_review_worker(settings, metrics)
    if metrics is None:
        worker.run_forever(stop, timedelta(seconds=settings.worker_poll_seconds))
    else:
        with serve_metrics(settings.worker_metrics, metrics.registry):
            worker.run_forever(stop, timedelta(seconds=settings.worker_poll_seconds))
