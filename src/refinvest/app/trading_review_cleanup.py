"""Bounded terminal retention cleanup for normalization jobs."""

import logging

from refinvest.app.config import get_settings
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository

_LOG = logging.getLogger(__name__)


def trading_review_cleanup_main() -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise ValueError("REFINVEST_DATABASE_URL is required.")
    deleted = PostgresTradingReviewJobRepository(settings.database_url).delete_cleanup_candidates(
        settings.trading_review_cleanup_batch_size
    )
    _LOG.info("trading_review_cleanup_completed deleted_jobs=%d", deleted)
