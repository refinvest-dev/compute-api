"""Analytics adapter composition for Job and existing shared delivery endpoints."""

from typing import Annotated

from fastapi import Depends

from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.application.analytics_jobs import AnalyticsJobRepository, AnalyticsRepository
from refinvest.persistence.postgres.trading_analytics_jobs import PostgresAnalyticsJobRepository


def get_optional_analytics_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AnalyticsRepository | None:
    if settings.database_url is None or settings.trading_review_active_job_limit is None:
        return None
    try:
        key = trading_review_payload_key(settings)
    except ValueError:
        return None
    return PostgresAnalyticsJobRepository(
        settings.database_url,
        key,
        active_job_limit=settings.trading_review_active_job_limit,
        max_payload_bytes=settings.trading_review_max_payload_bytes,
    )


def get_analytics_repository(
    repository: Annotated[AnalyticsRepository | None, Depends(get_optional_analytics_repository)],
) -> AnalyticsJobRepository:
    if repository is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="Analytics job store is unavailable.")
    return repository
