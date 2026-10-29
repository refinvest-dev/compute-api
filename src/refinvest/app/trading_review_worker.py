"""Runnable, lease-bound Trading Review normalization worker."""

from __future__ import annotations

import signal
from datetime import timedelta
from os import getpid
from socket import gethostname
from threading import Event

from refinvest.adapters.normalization.process_executor import ProcessIsolatedNormalizationExecutor
from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.application.normalization_execution import NormalizationExecutionSettings
from refinvest.application.normalization_worker import TradingReviewWorkerHost
from refinvest.observability.metrics import TradingReviewWorkerMetrics, serve_metrics
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository


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
    if settings.trading_review_max_payload_bytes <= 0:
        raise ValueError("REFINVEST_TRADING_REVIEW_MAX_PAYLOAD_BYTES must be positive.")
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
            max_payload_bytes=settings.trading_review_max_payload_bytes,
        ),
        ProcessIsolatedNormalizationExecutor(
            NormalizationExecutionSettings(
                settings.trading_review_timezone_database_version,
                settings.trading_review_identity_key,
                settings.trading_review_masking_key,
                settings.trading_review_artifact_hosts,
                settings.trading_review_max_payload_bytes,
            ),
            timedelta(seconds=settings.trading_review_execution_timeout_seconds),
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
