"""Lease, cancellation, retry and atomic publication coordination for Normalization."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from typing import Protocol

from refinvest.application.normalization_execution import (
    CompletedNormalization,
    NormalizationExecutor,
)
from refinvest.application.trading_review_jobs import (
    ResultPayloadLimitExceeded,
    SafeTradingReviewFailure,
    TradingReviewWorkerRepository,
)
from refinvest.contracts.normalization.result_integrity import ResultIntegrityError

_LOG = logging.getLogger("refinvest.app.trading_review_worker")
_METRICS_LOG = logging.getLogger("refinvest.observability.metrics")


class NormalizationWorkerMetrics(Protocol):
    def claimed(self) -> None: ...
    def queue_wait(self, seconds: float) -> None: ...
    def terminal(self, status: str, seconds: float) -> None: ...
    def retry(self) -> None: ...
    def failure(self, code: str) -> None: ...


def _safe_record(operation: object, *args: object) -> None:
    try:
        operation(*args)  # type: ignore[operator]
    except Exception:
        _METRICS_LOG.warning("metrics_record_failed")


class TradingReviewWorkerHost:
    def __init__(
        self,
        repository: TradingReviewWorkerRepository,
        executor: NormalizationExecutor,
        *,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        retention: timedelta,
        max_attempts: int = 3,
        metrics: NormalizationWorkerMetrics | None = None,
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
            _safe_record(self.metrics.claimed)
            _safe_record(
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
                    _safe_record(self.metrics.terminal, "COMPLETED", monotonic() - began)
            elif outcome.failure.code == "JOB_CANCELLED":
                committed = self.repository.mark_cancelled(claim)
                if committed and self.metrics is not None:
                    _safe_record(self.metrics.terminal, "CANCELLED", monotonic() - began)
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
                        _safe_record(self.metrics.retry)
                else:
                    committed = self.repository.mark_failed(claim, outcome.failure)
                    if committed and self.metrics is not None:
                        _safe_record(self.metrics.terminal, "FAILED", monotonic() - began)
                        _safe_record(self.metrics.failure, outcome.failure.code)
        except ResultPayloadLimitExceeded:
            failure = SafeTradingReviewFailure(
                "RESULT_PAYLOAD_LIMIT_EXCEEDED",
                "Normalization result exceeds the configured delivery limit.",
                False,
                "PUBLICATION",
            )
            self.repository.mark_failed(claim, failure)
            if self.metrics is not None:
                _safe_record(self.metrics.failure, failure.code)
        except ResultIntegrityError as error:
            failure = SafeTradingReviewFailure(
                error.code, "Normalization result integrity failed.", False, "PUBLICATION"
            )
            self.repository.mark_failed(claim, failure)
            if self.metrics is not None:
                _safe_record(self.metrics.failure, error.code)
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
