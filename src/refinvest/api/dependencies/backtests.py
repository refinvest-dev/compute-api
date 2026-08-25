"""Backtest submission dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, status

from refinvest.app.config import Settings, get_settings
from refinvest.application.backtests import BacktestJobRepository
from refinvest.persistence.postgres.backtest_jobs import PostgresBacktestJobRepository


def get_backtest_job_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> BacktestJobRepository:
    if settings.database_url is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL runtime state is not configured.",
        )
    return PostgresBacktestJobRepository(settings.database_url)
