"""Normalization repository composition shared with the existing delivery endpoints."""

from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException

from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.application.trading_review_jobs import TradingReviewJobRepository
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository


def get_trading_review_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TradingReviewJobRepository:
    if settings.database_url is None:
        raise HTTPException(status_code=503, detail="Job store is unavailable.")
    if settings.trading_review_active_job_limit is None:
        raise HTTPException(status_code=503, detail="Trading Review capacity is unavailable.")
    try:
        key = trading_review_payload_key(settings)
    except ValueError as error:
        raise HTTPException(
            status_code=503, detail="Terminal payload key is unavailable."
        ) from error
    return PostgresTradingReviewJobRepository(
        settings.database_url,
        key,
        active_job_limit=settings.trading_review_active_job_limit,
        grant_renewal_cutoff=timedelta(
            seconds=settings.trading_review_grant_renewal_cutoff_seconds
        ),
        grant_refresh_wait=timedelta(seconds=settings.trading_review_grant_refresh_wait_seconds),
        max_attempts=settings.trading_review_max_attempts,
        max_payload_bytes=settings.trading_review_max_payload_bytes,
    )
