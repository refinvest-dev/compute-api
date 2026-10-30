"""Analytics worker configuration, adapter composition and CLI entrypoint."""

from __future__ import annotations

import signal
from datetime import timedelta
from os import getpid
from socket import gethostname
from threading import Event

from refinvest.adapters.analytics.artifacts import download_inputs
from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.application.analytics_worker import (
    AnalyticsWorkerSettings,
    TradingAnalyticsWorkerHost,
)
from refinvest.persistence.postgres.trading_analytics_jobs import PostgresAnalyticsJobRepository


def create_trading_analytics_worker(settings: Settings) -> TradingAnalyticsWorkerHost:
    if settings.database_url is None or settings.trading_review_active_job_limit is None:
        raise ValueError("Analytics database and capacity settings are required.")
    repository = PostgresAnalyticsJobRepository(
        settings.database_url,
        trading_review_payload_key(settings),
        active_job_limit=settings.trading_review_active_job_limit,
        max_payload_bytes=settings.trading_review_max_payload_bytes,
    )
    return TradingAnalyticsWorkerHost(
        repository,
        AnalyticsWorkerSettings(
            settings.trading_review_artifact_hosts,
            settings.trading_analytics_implementation_digest,
            settings.trading_analytics_max_input_bytes,
        ),
        worker_id=settings.worker_id or f"{gethostname()}-{getpid()}",
        download_inputs=download_inputs,
    )


def trading_analytics_worker_main() -> None:
    shutdown = Event()

    def stop(_: int, __: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    settings = get_settings()
    create_trading_analytics_worker(settings).run_forever(
        shutdown, timedelta(seconds=settings.worker_poll_seconds)
    )
