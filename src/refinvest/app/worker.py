"""Runnable composition root for durable Backtest workers."""

import signal
from datetime import timedelta
from os import getpid
from socket import gethostname
from threading import Event

from refinvest.app.config import Settings, get_settings
from refinvest.application.backtest_executor import DailyBarBacktestExecutor
from refinvest.application.backtest_worker_host import BacktestWorkerHost
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)
from refinvest.persistence.postgres.backtest_jobs import PostgresBacktestJobRepository
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository


def create_backtest_worker(settings: Settings) -> BacktestWorkerHost:
    """Compose the worker from explicit runtime configuration and immutable adapters."""
    if not settings.database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for the backtest worker.")
    if not settings.engine_version.strip():
        raise ValueError("REFINVEST_ENGINE_VERSION must not be empty.")

    lease_duration = timedelta(seconds=settings.worker_lease_seconds)
    heartbeat_interval = timedelta(seconds=settings.worker_heartbeat_seconds)
    if heartbeat_interval >= lease_duration:
        raise ValueError("Worker heartbeat interval must be shorter than its lease duration.")

    jobs = PostgresBacktestJobRepository(settings.database_url)
    snapshots = PostgresDatasetSnapshotRepository(settings.database_url)
    executor = DailyBarBacktestExecutor(
        FilesystemSnapshotExecutionDataLoader(), settings.engine_version
    )
    return BacktestWorkerHost(
        settings.worker_id or _default_worker_id(),
        lease_duration,
        heartbeat_interval,
        jobs,
        snapshots,
        executor,
    )


def run_backtest_worker(settings: Settings, stop_requested: Event | None = None) -> None:
    """Run until SIGINT/SIGTERM; an in-flight lease is recoverable after shutdown."""
    stop = stop_requested or Event()
    _register_shutdown_signals(stop)
    worker = create_backtest_worker(settings)
    worker.run_forever(stop, timedelta(seconds=settings.worker_poll_seconds))


def worker_main() -> None:
    """Console-script entrypoint for the separate worker process."""
    run_backtest_worker(get_settings())


def _default_worker_id() -> str:
    return f"{gethostname()}-{getpid()}"


def _register_shutdown_signals(stop_requested: Event) -> None:
    def request_stop(_: int, __: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
