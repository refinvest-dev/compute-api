"""Deterministic import quality accounting."""

from __future__ import annotations

from collections import Counter

from refinvest.trading_review.model import (
    ArtifactCoverage,
    FuturesPositionEpisode,
    ImportQualityReport,
    QualityStatus,
    ReconciliationIssue,
    ReconciliationStatus,
    ReconstructionStatus,
    SourceArtifactRole,
    ValidationIssue,
)


def build_quality_report(
    *,
    trade_source_rows: int,
    position_source_rows: int,
    canonical_record_count: int,
    duplicate_count: int,
    episodes: tuple[FuturesPositionEpisode, ...],
    issues: tuple[ReconciliationIssue, ...],
    coverages: tuple[ArtifactCoverage, ...],
    validation_issues: tuple[ValidationIssue, ...],
) -> ImportQualityReport:
    reconstruction_counts = Counter(item.reconstruction_status for item in episodes)
    reconciliation_counts = Counter(item.reconciliation_status for item in episodes)
    issue_counts = Counter(item.code for item in issues)
    issue_counts.update(item.code for item in validation_issues)
    exclusions = Counter(reason for item in episodes for reason in item.exclusion_reasons)
    eligible_count = sum(item.eligible for item in episodes)
    excluded_count = len(episodes) - eligible_count
    effective_validation_issues = list(validation_issues)
    if trade_source_rows == 0 and position_source_rows == 0:
        effective_validation_issues.append(
            ValidationIssue(
                code="NO_TRADING_RECORDS",
                severity="WARNING",
                artifact_role=SourceArtifactRole.TRADE_HISTORY,
            )
        )
        issue_counts["NO_TRADING_RECORDS"] += 1
    quality_status = (
        QualityStatus.ACCEPTABLE
        if excluded_count == 0 and not effective_validation_issues
        else QualityStatus.ACCEPTABLE_WITH_EXCLUSIONS
    )
    return ImportQualityReport(
        quality_status=quality_status,
        source_row_counts=(
            (SourceArtifactRole.POSITION_HISTORY, position_source_rows),
            (SourceArtifactRole.TRADE_HISTORY, trade_source_rows),
        ),
        parsed_row_counts=(
            (SourceArtifactRole.POSITION_HISTORY, position_source_rows),
            (SourceArtifactRole.TRADE_HISTORY, trade_source_rows),
        ),
        canonical_record_count=canonical_record_count,
        duplicate_count=duplicate_count,
        conflict_count=0,
        reconstructed_episode_count=len(episodes),
        eligible_count=eligible_count,
        excluded_count=excluded_count,
        reconstruction_status_counts=tuple(
            (status, reconstruction_counts[status])
            for status in ReconstructionStatus
            if reconstruction_counts[status]
        ),
        reconciliation_status_counts=tuple(
            (status, reconciliation_counts[status])
            for status in ReconciliationStatus
            if reconciliation_counts[status]
        ),
        issue_code_counts=tuple(sorted(issue_counts.items())),
        exclusion_reason_counts=tuple(sorted(exclusions.items())),
        artifact_coverages=coverages,
        validation_issues=tuple(
            sorted(
                effective_validation_issues,
                key=lambda item: (
                    item.code,
                    item.artifact_role.value,
                    item.source_row_number or 0,
                    item.field or "",
                ),
            )
        ),
    )
