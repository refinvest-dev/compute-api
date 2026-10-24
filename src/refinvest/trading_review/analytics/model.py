"""Immutable input and logical result types for behavior metrics v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from refinvest.trading_review.decimal import ExactRational, ScaledDecimal
from refinvest.trading_review.model import (
    AllocationAction,
    Capability,
    Direction,
    MoneyAmount,
    ReconciliationStatus,
    ReconstructionStatus,
)


class MetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FindingEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_SAMPLE = "INELIGIBLE_SAMPLE"
    INELIGIBLE_METRIC_STATUS = "INELIGIBLE_METRIC_STATUS"


class Outcome(StrEnum):
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    WIN = "WIN"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AnalysisPeriod:
    start_inclusive: datetime
    end_exclusive: datetime

    def __post_init__(self) -> None:
        if self.start_inclusive.tzinfo is None or self.end_exclusive.tzinfo is None:
            raise ValueError("analysis period requires timezone-aware instants")
        if self.start_inclusive.utcoffset() != timedelta(
            0
        ) or self.end_exclusive.utcoffset() != timedelta(0):
            raise ValueError("analysis period must be pinned UTC instants")
        if self.start_inclusive >= self.end_exclusive:
            raise ValueError("analysis period must be non-empty")

    def contains(self, instant: datetime | None) -> bool:
        return instant is not None and self.start_inclusive <= instant < self.end_exclusive


@dataclass(frozen=True, slots=True)
class MetricMinimums:
    reentry_eligible_losses: int = 1
    exposure_pairs: int = 1
    prior_baseline_episodes: int = 3
    holding_wins: int = 2
    holding_losses: int = 2


@dataclass(frozen=True, slots=True)
class FindingMinimums:
    reentry_eligible_losses: int = 5
    exposure_pairs: int = 5
    holding_wins: int = 5
    holding_losses: int = 5


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    analysis_period: AnalysisPeriod
    review_timezone: str
    timezone_database_version: str
    metric_minimums: MetricMinimums = MetricMinimums()
    finding_minimums: FindingMinimums = FindingMinimums()
    schema_version: int = 1
    outcome_basis: str = "NET_TRADING_PNL"
    funding_treatment: str = "EXCLUDED"
    eligible_review_unit_policy: str = "COMPLETE_AND_RECONCILED_ONLY"
    reversal_treatment: str = "SEPARATE"
    reentry_window: str = "PT30M"
    baseline_policy: str = "PRIOR_PERIOD_SAME_INSTRUMENT_DIRECTION"

    def __post_init__(self) -> None:
        expected = (
            (self.schema_version, 1),
            (self.outcome_basis, "NET_TRADING_PNL"),
            (self.funding_treatment, "EXCLUDED"),
            (self.eligible_review_unit_policy, "COMPLETE_AND_RECONCILED_ONLY"),
            (self.reversal_treatment, "SEPARATE"),
            (self.reentry_window, "PT30M"),
            (self.baseline_policy, "PRIOR_PERIOD_SAME_INSTRUMENT_DIRECTION"),
        )
        if any(actual != required for actual, required in expected):
            raise ValueError("unsupported AnalysisConfig V1 policy")
        if not self.review_timezone or not self.timezone_database_version:
            raise ValueError("pinned timezone and database version are required")
        if any(
            value < 1
            for value in (
                self.metric_minimums.reentry_eligible_losses,
                self.metric_minimums.exposure_pairs,
                self.metric_minimums.prior_baseline_episodes,
                self.metric_minimums.holding_wins,
                self.metric_minimums.holding_losses,
                self.finding_minimums.reentry_eligible_losses,
                self.finding_minimums.exposure_pairs,
                self.finding_minimums.holding_wins,
                self.finding_minimums.holding_losses,
            )
        ):
            raise ValueError("sample minimums must be positive")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    episode_id: str
    allocation_record_ids: tuple[str, ...]
    canonical_record_ids: tuple[str, ...]
    source_rows: tuple[tuple[str, int, str], ...]  # artifact ID, row number, opaque row hash


@dataclass(frozen=True, slots=True)
class OutcomeClassification:
    episode_id: str
    outcome: Outcome
    net_trading_pnl: Decimal | None
    settlement_currency: str | None
    allocated_reported_pnl: tuple[tuple[str, Decimal, str], ...]
    allocated_fees: tuple[tuple[str, Decimal, str], ...]
    evidence: EvidenceReference


@dataclass(frozen=True, slots=True)
class Membership:
    episode_id: str
    role: str
    reasons: tuple[str, ...]
    evidence: EvidenceReference


@dataclass(frozen=True, slots=True)
class RelationCandidate:
    entry_id: str
    role: str
    opened_at: datetime | None
    evidence: EvidenceReference


@dataclass(frozen=True, slots=True)
class LossEntryRelation:
    loss_id: str
    closed_at: datetime
    candidates: tuple[RelationCandidate, ...]
    related_entry_ids: tuple[str, ...]
    elapsed_microseconds: int | None


@dataclass(frozen=True, slots=True)
class Distribution:
    count: int
    minimum: ExactRational | None
    p25: ExactRational | None
    median: ExactRational | None
    p75: ExactRational | None
    maximum: ExactRational | None
    mean: ExactRational | None


@dataclass(frozen=True, slots=True)
class ReentryValues:
    eligible_loss_count: int
    loss_with_entry_count: int
    related_entry_count: int
    same_symbol_count: int
    different_symbol_count: int
    same_instrument_related_entry_count: int
    different_instrument_related_entry_count: int
    entry_within_window_rate: ExactRational | None
    elapsed_time_distribution: Distribution | None


@dataclass(frozen=True, slots=True)
class ExposurePair:
    loss_id: str
    entry_id: str
    currency: str
    loss_notional: Decimal
    entry_notional: Decimal
    absolute_delta: Decimal
    ratio: ExactRational
    percentage_delta: ExactRational
    same_instrument: bool
    same_direction: bool
    baseline_episode_ids: tuple[str, ...]
    baseline_median: ExactRational | None
    baseline_ratio: ExactRational | None
    baseline_delta: ExactRational | None
    baseline_reason: str | None


@dataclass(frozen=True, slots=True)
class ExposureValues:
    pair_count: int
    increased_count: int
    equal_count: int
    decreased_count: int
    median_next_entry_vs_loss_episode_ratio: ExactRational | None
    ratio_distribution: Distribution | None
    percentage_delta_distribution: Distribution | None
    absolute_delta_by_currency: tuple[tuple[str, Distribution], ...]
    pairs: tuple[ExposurePair, ...]


@dataclass(frozen=True, slots=True)
class HoldingValues:
    win_count: int
    loss_count: int
    breakeven_count: int
    unavailable_count: int
    win_distribution: Distribution | None
    loss_distribution: Distribution | None
    median_difference_microseconds: ExactRational | None
    median_ratio: ExactRational | None
    median_ratio_reason: str | None


@dataclass(frozen=True, slots=True)
class MetricResult:
    definition_id: str
    definition_version: str
    analysis_config: AnalysisConfig
    status: MetricStatus
    finding_eligibility: FindingEligibility
    required_capabilities: tuple[Capability, ...]
    population_count: int
    eligible_count: int
    excluded_count: int
    excluded_reason_counts: tuple[tuple[str, int], ...]
    subject_membership: tuple[Membership, ...]
    comparison_membership: tuple[Membership, ...]
    excluded_membership: tuple[Membership, ...]
    evidence_references: tuple[EvidenceReference, ...]
    scalar_inputs: tuple[tuple[str, str, str], ...]
    values: ReentryValues | ExposureValues | HoldingValues
    relations: tuple[LossEntryRelation, ...] = ()
    outcome_classifications: tuple[OutcomeClassification, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalyticsEvidenceRow:
    """Only provenance fields present in the immutable Analytics ledger artifact."""

    artifact_id: str
    source_row_number: int
    source_row_hash: str


@dataclass(frozen=True, slots=True)
class AnalyticsCanonicalInput:
    record_id: str
    provenance: tuple[AnalyticsEvidenceRow, ...]


@dataclass(frozen=True, slots=True)
class AnalyticsExecutionInput(AnalyticsCanonicalInput):
    price: ScaledDecimal


@dataclass(frozen=True, slots=True)
class AnalyticsAllocationInput:
    execution_record_id: str
    action: AllocationAction
    allocated_fee: MoneyAmount
    allocated_reported_pnl: MoneyAmount
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalyticsCapabilityInput:
    capabilities: tuple[Capability, ...]
    stable_episode_time_available: bool


@dataclass(frozen=True, slots=True)
class AnalyticsEpisodeInput:
    episode_id: str
    instrument_id: str
    direction: Direction
    opened_at: datetime | None
    closed_at: datetime | None
    initial_quantity: ScaledDecimal | None
    net_trading_pnl: MoneyAmount | None
    reconstruction_status: ReconstructionStatus
    reconciliation_status: ReconciliationStatus
    allocations: tuple[AnalyticsAllocationInput, ...]
    capability: AnalyticsCapabilityInput
    evidence_record_ids: tuple[str, ...]


def utc(instant: datetime | None) -> datetime:
    if instant is None or instant.tzinfo is None:
        raise ValueError("execution instant must be timezone-aware")
    return instant.astimezone(UTC)
