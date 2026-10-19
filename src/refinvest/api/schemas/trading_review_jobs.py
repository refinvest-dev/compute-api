"""OpenAPI-shaped transport models for normalization jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from refinvest.app.trading_review_identity import NormalizationInputIdentity, TargetVersionSet


class StrictTransport(BaseModel):
    model_config = ConfigDict(populate_by_name=False, extra="forbid")


class ArtifactReadGrantTransport(StrictTransport):
    core_artifact_id: str = Field(alias="coreArtifactId", min_length=1)
    role: Literal["TRADE_HISTORY", "POSITION_HISTORY"]
    raw_sha256: str = Field(alias="rawSha256", pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(alias="byteSize", ge=1, le=52_428_800)
    content_type: Literal["text/csv", "application/vnd.ms-excel"] = Field(alias="contentType")
    access_uri: HttpUrl = Field(alias="accessUri")
    expires_at: datetime = Field(alias="expiresAt")


class PinnedInstrumentTermsTransport(StrictTransport):
    instrument_id: str = Field(alias="instrumentId", min_length=1)
    venue_symbol: str = Field(alias="venueSymbol", min_length=1)
    settlement_asset: str = Field(alias="settlementAsset", min_length=1)
    quantity_step: str = Field(alias="quantityStep", min_length=1)
    price_tick: str = Field(alias="priceTick", min_length=1)
    contract_multiplier: str = Field(alias="contractMultiplier", min_length=1)
    settlement_precision: int = Field(alias="settlementPrecision", ge=0)
    instrument_terms_version: str = Field(alias="instrumentTermsVersion", min_length=1)


class NormalizationResultDeliveryPolicyTransport(StrictTransport):
    policy_version: Literal["RESULT_DELIVERY_V1"] = Field(alias="policyVersion")
    mode: Literal["SINGLE_RESOURCE"]
    max_accepted_payload_bytes: int = Field(
        alias="maxAcceptedPayloadBytes", ge=1, le=2**63 - 1, strict=True
    )
    accepted_content_encodings: list[Literal["identity"]] = Field(
        alias="acceptedContentEncodings", min_length=1, max_length=1
    )


class CreateTradingNormalizationJobTransport(StrictTransport):
    core_logical_job_id: str = Field(alias="coreLogicalJobId", min_length=1)
    core_normalization_run_id: str = Field(alias="coreNormalizationRunId", min_length=1)
    core_import_session_id: str = Field(alias="coreImportSessionId", min_length=1)
    input_identity: NormalizationInputIdentity = Field(alias="inputIdentity")
    expected_input_set_hash: str = Field(alias="expectedInputSetHash", pattern=r"^[a-f0-9]{64}$")
    result_delivery_policy: NormalizationResultDeliveryPolicyTransport = Field(
        alias="resultDeliveryPolicy"
    )
    artifact_access_grants: list[ArtifactReadGrantTransport] = Field(
        alias="artifactAccessGrants", min_length=2, max_length=2
    )
    pinned_instrument_terms: list[PinnedInstrumentTermsTransport] = Field(
        alias="pinnedInstrumentTerms"
    )
    retry_policy_version: str = Field(alias="retryPolicyVersion", min_length=1)
    deletion_generation: int = Field(alias="deletionGeneration", ge=0)
    attempt_token: str = Field(alias="attemptToken", min_length=1)

    @model_validator(mode="after")
    def matching_artifact_identity(self) -> CreateTradingNormalizationJobTransport:
        identity = self.input_identity
        if self.core_import_session_id != identity.source_import_session_ids[0]:
            raise ValueError("Core Import Session identity differs from input identity.")
        if {grant.role for grant in self.artifact_access_grants} != set(identity.artifacts_by_role):
            raise ValueError("Artifact grants require exactly one grant per role.")
        for grant in self.artifact_access_grants:
            artifact = identity.artifacts_by_role[grant.role]
            if (
                grant.core_artifact_id != artifact.artifact_id
                or grant.raw_sha256 != artifact.sha256
            ):
                raise ValueError("Artifact grant identity differs from input identity.")
        return self


class UpdateTradingNormalizationJobGrantsTransport(StrictTransport):
    mode: Literal["REPLACE_QUEUED_GRANTS", "START_RETRY_ATTEMPT"]
    expected_grant_revision: int = Field(alias="expectedGrantRevision", ge=0)
    expected_attempt_token: str = Field(alias="expectedAttemptToken", min_length=1)
    next_attempt_token: str = Field(alias="nextAttemptToken", min_length=1)
    expected_input_set_hash: str = Field(alias="expectedInputSetHash", pattern=r"^[a-f0-9]{64}$")
    version_set_hash: str = Field(alias="versionSetHash", pattern=r"^[a-f0-9]{64}$")
    deletion_generation: int = Field(alias="deletionGeneration", ge=0)
    artifact_access_grants: list[ArtifactReadGrantTransport] = Field(
        alias="artifactAccessGrants", min_length=2, max_length=2
    )


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
    core_normalization_run_id: str = Field(alias="coreNormalizationRunId", min_length=1)
    expected_input_set_hash: str = Field(alias="expectedInputSetHash", pattern=r"^[a-f0-9]{64}$")
    input_set_hash: str | None = Field(
        default=None, alias="inputSetHash", pattern=r"^[a-f0-9]{64}$"
    )
    hash_specification_version: str = Field(alias="hashSpecificationVersion", min_length=1)
    target_version_set: TargetVersionSet = Field(alias="targetVersionSet")
    grant_revision: int = Field(alias="grantRevision", ge=0)


class ComputeTerminalResultDescriptorTransport(StrictTransport):
    job_id: str = Field(alias="jobId")
    core_logical_job_id: str = Field(alias="coreLogicalJobId")
    core_normalization_run_id: str = Field(alias="coreNormalizationRunId", min_length=1)
    payload_id: str = Field(alias="payloadId")
    transport_mode: Literal["SINGLE_RESOURCE"] = Field(alias="transportMode")
    result_delivery_policy_version: Literal["RESULT_DELIVERY_V1"] = Field(
        alias="resultDeliveryPolicyVersion"
    )
    media_type: Literal["application/vnd.refinvest.trading-normalization+json"] = Field(
        alias="mediaType"
    )
    content_encoding: Literal["identity"] = Field(alias="contentEncoding")
    byte_size: int = Field(alias="byteSize", ge=1)
    payload_sha256: str = Field(alias="payloadSha256", pattern=r"^[a-f0-9]{64}$")
    input_set_hash: str = Field(alias="inputSetHash", pattern=r"^[a-f0-9]{64}$")
    hash_specification_version: Literal["TRADING_INPUT_SET_HASH_V1"] = Field(
        alias="hashSpecificationVersion"
    )
    target_version_set: TargetVersionSet = Field(alias="targetVersionSet")
    reconciliation_manifest_hash: str = Field(
        alias="reconciliationManifestHash", pattern=r"^[a-f0-9]{64}$"
    )
    result_hash: str = Field(alias="resultHash", pattern=r"^[a-f0-9]{64}$")
    result_schema_version: Literal["TRADING_NORMALIZATION_RESULT_V1"] = Field(
        alias="resultSchemaVersion"
    )
    result_hash_specification_version: Literal["TRADING_NORMALIZATION_RESULT_HASH_V1"] = Field(
        alias="resultHashSpecificationVersion"
    )
    counts: dict[str, int]
    section_digests: list[dict[str, object]] = Field(
        alias="sectionDigests", min_length=9, max_length=9
    )
    created_at: datetime = Field(alias="createdAt")
    expires_at: datetime = Field(alias="expiresAt")
    attempt_token: str = Field(alias="attemptToken", min_length=1)
    deletion_generation: int = Field(alias="deletionGeneration", ge=0)


class AcknowledgeTradingReviewJobTransport(StrictTransport):
    core_aggregate_id: str = Field(alias="coreAggregateId", min_length=1)
    payload_id: str = Field(alias="payloadId", min_length=1)
    payload_sha256: str = Field(alias="payloadSha256", pattern=r"^[a-f0-9]{64}$")
    terminal_result_hash: str = Field(alias="terminalResultHash", pattern=r"^[a-f0-9]{64}$")
    attempt_token: str = Field(alias="attemptToken", min_length=1)
    deletion_generation: int = Field(alias="deletionGeneration", ge=0)
    disposition: Literal["PERSISTED", "DISCARDED_STALE"]
    persisted_at: datetime | None = Field(default=None, alias="persistedAt")
    discarded_at: datetime | None = Field(default=None, alias="discardedAt")
    current_deletion_generation: int | None = Field(default=None, alias="currentDeletionGeneration")
