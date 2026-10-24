"""Purge expired Analytics terminal bytes and bounded job metadata."""

from __future__ import annotations

import logging

from refinvest.app.config import get_settings, trading_review_payload_key
from refinvest.persistence.postgres.trading_analytics_jobs import PostgresAnalyticsJobRepository

_LOG = logging.getLogger(__name__)


def trading_analytics_cleanup_main() -> None:
    settings = get_settings()
    if settings.database_url is None or settings.trading_review_active_job_limit is None:
        raise ValueError("Analytics database and capacity settings are required.")
    repository = PostgresAnalyticsJobRepository(
        settings.database_url,
        trading_review_payload_key(settings),
        active_job_limit=settings.trading_review_active_job_limit,
        max_payload_bytes=settings.trading_review_max_payload_bytes,
    )
    payloads, jobs = repository.cleanup(settings.trading_review_cleanup_batch_size)
    _LOG.info("trading_analytics_cleanup_completed payloads=%d jobs=%d", payloads, jobs)
