"""Independent observed coverage for Trade and Position artifacts."""

from __future__ import annotations

from refinvest.trading_review.model import (
    ArtifactCoverage,
    ExecutionRecord,
    SourceArtifactRole,
    VenuePositionSummaryRecord,
)


def compute_coverages(
    executions: tuple[ExecutionRecord, ...],
    summaries: tuple[VenuePositionSummaryRecord, ...],
    *,
    trade_row_count: int,
    position_row_count: int,
    source_timezone: str,
) -> tuple[ArtifactCoverage, ArtifactCoverage]:
    trade_times = tuple(record.occurred_at for record in executions)
    position_opened = tuple(record.opened_at for record in summaries)
    position_closed = tuple(record.closed_at for record in summaries)
    return (
        ArtifactCoverage(
            role=SourceArtifactRole.TRADE_HISTORY,
            row_count=trade_row_count,
            earliest_observed_at=min(trade_times, default=None),
            latest_observed_at=max(trade_times, default=None),
            source_timezone=source_timezone,
        ),
        ArtifactCoverage(
            role=SourceArtifactRole.POSITION_HISTORY,
            row_count=position_row_count,
            earliest_observed_at=min(position_opened, default=None),
            latest_observed_at=max(position_closed, default=None),
            source_timezone=source_timezone,
        ),
    )
