import json
import logging
from datetime import timedelta
from importlib import import_module

import pytest

from refinvest.app.backtest_job_cleanup import (
    backtest_job_cleanup_main,
    cleanup_expired_backtest_jobs,
)
from refinvest.app.config import Settings


class FakeJobs:
    def __init__(self, deleted: int = 0) -> None:
        self.deleted = deleted
        self.calls: list[tuple[timedelta, int]] = []

    def delete_expired_terminal_jobs(self, retention: timedelta, batch_size: int) -> int:
        self.calls.append((retention, batch_size))
        return self.deleted


def test_cleanup_uses_configured_retention_and_bounded_batch() -> None:
    jobs = FakeJobs(deleted=4)

    assert cleanup_expired_backtest_jobs(
        Settings(
            None,
            "postgresql://example",
            backtest_job_retention_seconds=90_000,
            backtest_job_cleanup_batch_size=500,
        ),
        jobs,
    ) == 4

    assert jobs.calls == [(timedelta(seconds=90_000), 500)]


def test_cleanup_requires_database_configuration() -> None:
    with pytest.raises(ValueError, match="REFINVEST_DATABASE_URL"):
        cleanup_expired_backtest_jobs(Settings(None), FakeJobs())


def test_cleanup_entrypoint_emits_structured_completion_event(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_module = import_module("refinvest.app.backtest_job_cleanup")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(cleanup_module, "get_settings", lambda: Settings(None, "postgresql://example"))
    monkeypatch.setattr(cleanup_module, "cleanup_expired_backtest_jobs", lambda _: 3)

    backtest_job_cleanup_main()

    assert json.loads(caplog.records[-1].message) == {
        "event": "backtest_cleanup_completed",
        "deleted_count": 3,
    }
