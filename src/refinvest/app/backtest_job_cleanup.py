"""Bounded cleanup of expired Compute backtest runtime jobs."""

from datetime import timedelta
from typing import Protocol

from refinvest.app.config import Settings, get_settings
from refinvest.persistence.postgres.backtest_jobs import PostgresBacktestJobRepository


class TerminalJobCleanupRepository(Protocol):
    def delete_expired_terminal_jobs(self, retention: timedelta, batch_size: int) -> int: ...


def cleanup_expired_backtest_jobs(
    settings: Settings, repository: TerminalJobCleanupRepository | None = None
) -> int:
    """Delete one bounded terminal-job batch using PostgreSQL's clock."""
    if not settings.database_url:
        raise ValueError("REFINVEST_DATABASE_URL is required for backtest job cleanup.")
    jobs = repository or PostgresBacktestJobRepository(settings.database_url)
    return jobs.delete_expired_terminal_jobs(
        timedelta(seconds=settings.backtest_job_retention_seconds),
        settings.backtest_job_cleanup_batch_size,
    )


def backtest_job_cleanup_main() -> None:
    """Console entrypoint used by the hourly systemd timer."""
    deleted = cleanup_expired_backtest_jobs(get_settings())
    print(f"deleted_backtest_jobs={deleted}")
