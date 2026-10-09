"""OpenAPI-shaped transport models for normalization jobs."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictTransport(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ArtifactReadGrantTransport(StrictTransport):
    core_artifact_id: str = Field(alias="coreArtifactId", min_length=1)
    role: Literal["TRADE_HISTORY", "POSITION_HISTORY"]
    raw_sha256: str = Field(alias="rawSha256", pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(alias="byteSize", ge=1, le=52_428_800)
    content_type: Literal["text/csv", "application/vnd.ms-excel"] = Field(alias="contentType")
    access_uri: HttpUrl = Field(alias="accessUri")
    expires_at: datetime = Field(alias="expiresAt")


class TradingBookProductTransport(StrictTransport):
    venue: Literal["BINANCE"]
    product_family: Literal["LINEAR_PERPETUAL"] = Field(alias="productFamily")
    position_mode: Literal["ONE_WAY"] = Field(alias="positionMode")
    settlement_asset: Literal["USDT"] = Field(alias="settlementAsset")


class PinnedInstrumentTermsTransport(StrictTransport):
    instrument_id: str = Field(alias="instrumentId", min_length=1)
    venue_symbol: str = Field(alias="venueSymbol", min_length=1)
    settlement_asset: str = Field(alias="settlementAsset", min_length=1)
    quantity_step: str = Field(alias="quantityStep", min_length=1)
    price_tick: str = Field(alias="priceTick", min_length=1)
    contract_multiplier: str = Field(alias="contractMultiplier", min_length=1)
    settlement_precision: int = Field(alias="settlementPrecision", ge=0)
    instrument_terms_version: str = Field(alias="instrumentTermsVersion", min_length=1)


class NormalizerVersionSetTransport(StrictTransport):
    source_dialect_versions_by_artifact_role: dict[str, str] = Field(
        alias="sourceDialectVersionsByArtifactRole"
    )
    adapter_version: str = Field(alias="adapterVersion", min_length=1)
    normalizer_version: str = Field(alias="normalizerVersion", min_length=1)
    canonical_schema_version: str = Field(alias="canonicalSchemaVersion", min_length=1)
    reconstruction_policy_version: str = Field(alias="reconstructionPolicyVersion", min_length=1)
    reconciliation_version: str = Field(alias="reconciliationVersion", min_length=1)
    review_unit_schema_version: str = Field(alias="reviewUnitSchemaVersion", min_length=1)
    evidence_schema_version: str = Field(alias="evidenceSchemaVersion", min_length=1)
    implementation_digest: str = Field(alias="implementationDigest", min_length=1)
    version_set_hash: str = Field(alias="versionSetHash", pattern=r"^[a-f0-9]{64}$")


class CreateTradingNormalizationJobTransport(StrictTransport):
    core_logical_job_id: str = Field(alias="coreLogicalJobId", min_length=1)
    core_import_session_id: str = Field(alias="coreImportSessionId", min_length=1)
    book_product: TradingBookProductTransport = Field(alias="bookProduct")
    artifacts: list[ArtifactReadGrantTransport] = Field(min_length=2, max_length=2)
    source_timezone: str = Field(alias="sourceTimezone", min_length=1)
    review_timezone: str = Field(alias="reviewTimezone", min_length=1)
    target_version_set: NormalizerVersionSetTransport = Field(alias="targetVersionSet")
    pinned_instrument_terms: list[PinnedInstrumentTermsTransport] = Field(
        alias="pinnedInstrumentTerms"
    )
    expected_input_hash: str = Field(alias="expectedInputHash", pattern=r"^[a-f0-9]{64}$")
    deletion_generation: int = Field(alias="deletionGeneration", ge=0)
    attempt_token: str = Field(alias="attemptToken", min_length=1)


class TradingComputeJobAcceptedTransport(StrictTransport):
    job_id: str = Field(alias="jobId")
    core_logical_job_id: str = Field(alias="coreLogicalJobId")
    kind: Literal["NORMALIZATION_RECONCILIATION"]
    status: Literal["PENDING"]
    created_at: datetime = Field(alias="createdAt")


class TradingStructuredFailureTransport(StrictTransport):
    code: str
    message: str
    retryable: bool
    details: list[dict[str, object]] = Field(default_factory=lambda: list[dict[str, object]]())


class TradingComputeJobStatusTransport(StrictTransport):
    job_id: str = Field(alias="jobId")
    core_logical_job_id: str = Field(alias="coreLogicalJobId")
    kind: Literal["NORMALIZATION_RECONCILIATION"]
    progress_stage: str = Field(alias="progressStage")
    attempt: int = Field(ge=1)
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(alias="startedAt")
    updated_at: datetime = Field(alias="updatedAt")
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
    terminal_payload_id: str | None = Field(default=None, alias="terminalPayloadId")
    result_hash: str | None = Field(default=None, alias="resultHash")
    terminal_at: datetime | None = Field(default=None, alias="terminalAt")
    failure: TradingStructuredFailureTransport | None = None


class ComputeTerminalResultDescriptorTransport(StrictTransport):
    job_id: str = Field(alias="jobId")
    core_logical_job_id: str = Field(alias="coreLogicalJobId")
    payload_id: str = Field(alias="payloadId")
    media_type: Literal["application/vnd.refinvest.trading-normalization+json"] = Field(
        alias="mediaType"
    )
    byte_size: int = Field(alias="byteSize", ge=1)
    payload_sha256: str = Field(alias="payloadSha256", pattern=r"^[a-f0-9]{64}$")
    input_hash: str = Field(alias="inputHash", pattern=r"^[a-f0-9]{64}$")
    version_set_hash: str = Field(alias="versionSetHash", pattern=r"^[a-f0-9]{64}$")
    result_hash: str = Field(alias="resultHash", pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(alias="createdAt")
    expires_at: datetime = Field(alias="expiresAt")


class AcknowledgeTradingReviewJobTransport(StrictTransport):
    core_aggregate_id: str = Field(alias="coreAggregateId", min_length=1)
    payload_id: str = Field(alias="payloadId", min_length=1)
    payload_sha256: str = Field(alias="payloadSha256", pattern=r"^[a-f0-9]{64}$")
    terminal_result_hash: str = Field(alias="terminalResultHash", pattern=r"^[a-f0-9]{64}$")
    persisted_at: datetime = Field(alias="persistedAt")
