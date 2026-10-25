"""Immutable Trading Review domain model, separate from backtest execution types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from refinvest.trading_review.decimal import ExactRational, ScaledDecimal
from refinvest.trading_review.hashing import sha256_hex


class SourceArtifactRole(StrEnum):
    TRADE_HISTORY = "TRADE_HISTORY"
    POSITION_HISTORY = "POSITION_HISTORY"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class MarginMode(StrEnum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class FeeKind(StrEnum):
    COST = "COST"
    REBATE = "REBATE"
    ZERO = "ZERO"


class AllocationAction(StrEnum):
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    REVERSAL_CLOSE = "REVERSAL_CLOSE"
    REVERSAL_OPEN = "REVERSAL_OPEN"


class ReconstructionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    LEFT_CENSORED = "LEFT_CENSORED"
    RIGHT_CENSORED = "RIGHT_CENSORED"
    INCONSISTENT = "INCONSISTENT"
    UNSUPPORTED = "UNSUPPORTED"


class ReconciliationStatus(StrEnum):
    EXACT = "EXACT"
    WITHIN_ROUNDING_TOLERANCE = "WITHIN_ROUNDING_TOLERANCE"
    AMBIGUOUS = "AMBIGUOUS"
    INCOMPLETE_TRADES = "INCOMPLETE_TRADES"
    SUMMARY_MISMATCH = "SUMMARY_MISMATCH"
    UNSUPPORTED_OVERLAP = "UNSUPPORTED_OVERLAP"


class Capability(StrEnum):
    CLOSED_OUTCOME = "CLOSED_OUTCOME"
    INITIAL_EXPOSURE = "INITIAL_EXPOSURE"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    POSITION_INCREASES = "POSITION_INCREASES"
    FEES = "FEES"
    FUNDING_PAYMENTS = "FUNDING_PAYMENTS"
    MARKET_PRICE_PATH = "MARKET_PRICE_PATH"
    VENUE_REPORTED_PNL = "VENUE_REPORTED_PNL"
    VENUE_POSITION_SUMMARY = "VENUE_POSITION_SUMMARY"
    POSITION_MODE_USER_DECLARED = "POSITION_MODE_USER_DECLARED"
    POSITION_MODE_OBSERVED_COMPATIBLE = "POSITION_MODE_OBSERVED_COMPATIBLE"
    STABLE_EPISODE_TIME = "STABLE_EPISODE_TIME"
    EXECUTION_ORIGIN_KNOWN = "EXECUTION_ORIGIN_KNOWN"


class QualityStatus(StrEnum):
    ACCEPTABLE = "ACCEPTABLE"
    ACCEPTABLE_WITH_EXCLUSIONS = "ACCEPTABLE_WITH_EXCLUSIONS"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    artifact_id: str = field(metadata={"hash": False})
    role: SourceArtifactRole
    dialect: str
    content: bytes = field(repr=False, metadata={"hash": False})

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.content)


@dataclass(frozen=True, slots=True)
class ArtifactCoverage:
    role: SourceArtifactRole
    row_count: int
    earliest_observed_at: datetime | None
    latest_observed_at: datetime | None
    source_timezone: str
    timestamp_precision: str = "SECOND"
    completeness_assertion: str = "NOT_PROVEN"


@dataclass(frozen=True, slots=True)
class SourceRowProvenance:
    artifact_id: str = field(metadata={"hash": False})
    artifact_role: SourceArtifactRole
    artifact_content_hash: str
    source_row_number: int
    source_row_hash: str


@dataclass(frozen=True, slots=True)
class InstrumentDescriptor:
    instrument_id: str
    venue_symbol: str
    settlement_asset: str
    quantity_step: ScaledDecimal
    price_tick: ScaledDecimal
    contract_multiplier: ScaledDecimal
    settlement_precision: int
    instrument_terms_version: str
    venue: str = "BINANCE"
    product_family: str = "LINEAR_PERPETUAL"


@dataclass(frozen=True, slots=True)
class FeeValuation:
    source_asset: str
    settlement_asset: str
    rate: ScaledDecimal
    valuation_version: str


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    record_id: str
    record_hash: str
    source_execution_key: str
    instrument_id: str
    source_symbol: str
    occurred_at: datetime
    side: Side
    price: ScaledDecimal
    quantity: ScaledDecimal
    source_amount: ScaledDecimal
    buyer: bool
    maker: bool
    execution_origin: str
    provenance: tuple[SourceRowProvenance, ...]
    _trade_id_lexeme: str = field(repr=False, metadata={"hash": False})
    _trade_id_numeric: int = field(repr=False, metadata={"hash": False})

    @property
    def canonical_trade_order(self) -> int:
        return self._trade_id_numeric


@dataclass(frozen=True, slots=True)
class FeeRecord:
    record_id: str
    record_hash: str
    source_execution_key: str
    instrument_id: str
    occurred_at: datetime
    source_amount: ScaledDecimal
    asset: str
    fee_kind: FeeKind
    balance_effect: ScaledDecimal
    provenance: tuple[SourceRowProvenance, ...]


@dataclass(frozen=True, slots=True)
class VenueReportedPnlRecord:
    record_id: str
    record_hash: str
    source_execution_key: str
    instrument_id: str
    occurred_at: datetime
    realized_pnl: ScaledDecimal
    settlement_asset: str
    provenance: tuple[SourceRowProvenance, ...]


@dataclass(frozen=True, slots=True)
class VenuePositionSummaryRecord:
    record_id: str
    record_hash: str
    source_identity: str
    instrument_id: str
    source_symbol: str
    opened_at: datetime
    closed_at: datetime
    direction: Direction
    margin_mode: MarginMode
    entry_price: ScaledDecimal
    average_close_price: ScaledDecimal
    max_open_interest: ScaledDecimal
    closed_volume: ScaledDecimal
    closing_pnl: ScaledDecimal
    source_status: str
    provenance: tuple[SourceRowProvenance, ...]


CanonicalRecord: TypeAlias = (
    ExecutionRecord | FeeRecord | VenueReportedPnlRecord | VenuePositionSummaryRecord
)


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    amount: ScaledDecimal
    asset: str


@dataclass(frozen=True, slots=True)
class PositionAllocation:
    execution_record_id: str
    source_execution_key: str
    episode_id: str
    action: AllocationAction
    allocated_quantity: ScaledDecimal
    allocated_fee: MoneyAmount
    allocated_reported_pnl: MoneyAmount
    reversal_group_id: str | None
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    capabilities: tuple[Capability, ...]
    gross_pnl_available: bool
    fee_adjusted_outcome_available: bool
    holding_duration_available: bool
    stable_episode_time_available: bool
    reconciliation_available: bool
    funding_inclusive_outcome_available: bool
    unsupported_fee_assets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FuturesPositionEpisode:
    episode_id: str
    instrument_id: str
    source_symbol: str
    direction: Direction
    opened_at: datetime | None
    closed_at: datetime | None
    initial_quantity: ScaledDecimal | None
    max_open_quantity: ScaledDecimal | None
    opening_volume: ScaledDecimal
    closing_volume: ScaledDecimal
    average_entry_price: ExactRational | None
    average_close_price: ExactRational | None
    gross_realized_pnl: MoneyAmount | None
    fees_by_asset: tuple[MoneyAmount, ...]
    net_trading_pnl: MoneyAmount | None
    reconstruction_status: ReconstructionStatus
    reconciliation_status: ReconciliationStatus
    allocations: tuple[PositionAllocation, ...]
    capability: CapabilitySnapshot
    eligible: bool
    exclusion_reasons: tuple[str, ...]
    evidence_record_ids: tuple[str, ...]
    _entry_price_source_scale: int = field(default=0, repr=False, metadata={"hash": False})
    _exit_price_source_scale: int = field(default=0, repr=False, metadata={"hash": False})

    @property
    def entry_price_source_scale(self) -> int:
        return self._entry_price_source_scale

    @property
    def exit_price_source_scale(self) -> int:
        return self._exit_price_source_scale


@dataclass(frozen=True, slots=True)
class ReconciliationDelta:
    field: str
    actual: ScaledDecimal
    expected: ScaledDecimal
    absolute_delta: ScaledDecimal
    tolerance: ScaledDecimal


@dataclass(frozen=True, slots=True)
class ReconciliationMatch:
    summary_record_id: str
    episode_id: str | None
    status: ReconciliationStatus
    candidate_episode_ids: tuple[str, ...]
    deltas: tuple[ReconciliationDelta, ...]
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    code: str
    severity: str
    episode_id: str | None
    summary_record_id: str | None
    evidence_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationManifest:
    manifest_schema_version: str
    input_hash: str
    canonical_record_set_hash: str
    instrument_terms_hash: str
    version_set_hash: str
    reconstruction_policy_version: str
    reconciliation_version: str
    source_artifact_content_hashes: tuple[tuple[SourceArtifactRole, str], ...]
    artifact_coverages: tuple[ArtifactCoverage, ...]
    episodes: tuple[FuturesPositionEpisode, ...]
    matches: tuple[ReconciliationMatch, ...]
    issues: tuple[ReconciliationIssue, ...]
    quantity_conservation_passed: bool
    fee_conservation_passed: bool
    pnl_conservation_passed: bool


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    severity: str
    artifact_role: SourceArtifactRole
    source_row_number: int | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class ImportQualityReport:
    quality_status: QualityStatus
    source_row_counts: tuple[tuple[SourceArtifactRole, int], ...]
    parsed_row_counts: tuple[tuple[SourceArtifactRole, int], ...]
    canonical_record_count: int
    duplicate_count: int
    conflict_count: int
    reconstructed_episode_count: int
    eligible_count: int
    excluded_count: int
    reconstruction_status_counts: tuple[tuple[ReconstructionStatus, int], ...]
    reconciliation_status_counts: tuple[tuple[ReconciliationStatus, int], ...]
    issue_code_counts: tuple[tuple[str, int], ...]
    exclusion_reason_counts: tuple[tuple[str, int], ...]
    artifact_coverages: tuple[ArtifactCoverage, ...]
    validation_issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class NormalizationVersionSet:
    source_dialect_versions_by_artifact_role: tuple[tuple[SourceArtifactRole, str], ...]
    adapter_version: str
    normalizer_version: str
    canonical_schema_version: str
    reconstruction_policy_version: str
    reconciliation_version: str
    review_unit_schema_version: str
    evidence_schema_version: str
    implementation_digest: str
    version_set_hash: str = field(metadata={"hash": False})


@dataclass(frozen=True, slots=True)
class NormalizationRequest:
    artifacts: tuple[SourceArtifact, ...]
    source_timezone: str
    timezone_database_version: str
    declared_position_mode: str
    instruments: tuple[InstrumentDescriptor, ...]
    version_set: NormalizationVersionSet
    stable_identity_key: bytes = field(repr=False, metadata={"hash": False})
    identity_key_version: str = "TRADING_IDENTITY_KEY_V1"
    fee_valuations: tuple[FeeValuation, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    input_hash: str
    version_set: NormalizationVersionSet
    instrument_terms_hash: str
    artifact_coverages: tuple[ArtifactCoverage, ...]
    canonical_records: tuple[CanonicalRecord, ...]
    episodes: tuple[FuturesPositionEpisode, ...]
    reconciliation_manifest: ReconciliationManifest
    capability: CapabilitySnapshot
    quality_report: ImportQualityReport
    canonical_record_set_hash: str
