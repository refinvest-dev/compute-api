import importlib
from datetime import timedelta
from threading import Event

import pytest

from refinvest.app.config import Settings
from refinvest.app.worker import create_backtest_worker, run_backtest_worker
from refinvest.application.backtest_worker_host import BacktestWorkerHost


def test_create_worker_requires_database_configuration() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_backtest_worker(Settings(internal_api_key=None))


def test_create_worker_rejects_heartbeat_that_cannot_renew_lease() -> None:
    with pytest.raises(ValueError, match="shorter"):
        create_backtest_worker(
            Settings(
                internal_api_key=None,
                database_url="postgresql://example",
                worker_lease_seconds=30,
                worker_heartbeat_seconds=30,
            )
        )


def test_create_worker_composes_without_connecting_to_database() -> None:
    worker = create_backtest_worker(
        Settings(
            internal_api_key=None,
            database_url="postgresql://example",
            engine_version="engine-v1",
            worker_id="worker-test",
        )
    )

    assert isinstance(worker, BacktestWorkerHost)


def test_run_worker_passes_configured_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Event, float]] = []

    class FakeWorker:
        def run_forever(self, stop: Event, poll_interval: timedelta) -> None:
            calls.append((stop, poll_interval.total_seconds()))

    worker_module = importlib.import_module("refinvest.app.worker")
    monkeypatch.setattr(worker_module, "create_backtest_worker", lambda _: FakeWorker())
    monkeypatch.setattr(worker_module, "_register_shutdown_signals", lambda _: None)
    stop = Event()

    run_backtest_worker(Settings(None, "postgresql://example", worker_poll_seconds=3), stop)

    assert calls == [(stop, 3)]
