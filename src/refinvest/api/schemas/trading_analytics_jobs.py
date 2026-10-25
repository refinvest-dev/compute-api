"""Strict internal Analytics Job Protocol V1 request models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA = r"^[0-9a-f]{64}$"
Capability = Literal[
    "CLOSED_OUTCOME",
    "INITIAL_EXPOSURE",
    "MAX_EXPOSURE",
    "POSITION_INCREASES",
    "FEES",
    "FUNDING_PAYMENTS",
    "MARKET_PRICE_PATH",
    "VENUE_REPORTED_PNL",
    "POSITION_MODE_USER_DECLARED",
    "POSITION_MODE_OBSERVED_COMPATIBLE",
    "VENUE_POSITION_SUMMARY",
    "EXECUTION_ORIGIN_KNOWN",
    "STABLE_EPISODE_TIME",
]


class AnalyticsTransport(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)


class CorePayloadReferenceTransport(AnalyticsTransport):
    core_reference_id: str = Field(alias="coreReferenceId", min_length=1)
    media_type: str = Field(alias="mediaType", min_length=1)
    byte_size: int = Field(alias="byteSize", ge=1, le=9_007_199_254_740_991, strict=True)
    sha256: str = Field(pattern=SHA)
    access_uri: str = Field(alias="accessUri", min_length=1)
    expires_at: datetime = Field(alias="expiresAt")

    @field_validator("access_uri")
    @classmethod
    def valid_grant_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Invalid artifact grant URI.")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Invalid artifact grant URI.")
        return value


class ArtifactIdentityTransport(AnalyticsTransport):
    core_reference_id: str = Field(alias="coreReferenceId", min_length=1)
    media_type: str = Field(alias="mediaType", min_length=1)
    byte_size: int = Field(alias="byteSize", ge=1, le=9_007_199_254_740_991, strict=True)
    sha256: str = Field(pattern=SHA)


class ManifestIdentityTransport(ArtifactIdentityTransport):
    manifest_hash: str = Field(alias="manifestHash", pattern=SHA)


class VersionSetTransport(AnalyticsTransport):
    metric_definition_versions_by_metric_id: dict[
        Literal[
            "ENTRY_WITHIN_WINDOW_AFTER_LOSS",
            "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS",
            "WIN_LOSS_HOLDING_DURATION_DIFFERENCE",
        ],
        str,
    ] = Field(alias="metricDefinitionVersionsByMetricId")
    finding_rule_set_version: Literal["TRADING_FINDING_RULES_DISABLED_V1"] = Field(
        alias="findingRuleSetVersion"
    )
    analysis_config_version: Literal["ANALYSIS_CONFIG_V1"] = Field(alias="analysisConfigVersion")
    evidence_schema_version: str = Field(alias="evidenceSchemaVersion", min_length=1)
    implementation_digest: str = Field(alias="implementationDigest", min_length=1)
    version_set_hash: str = Field(alias="versionSetHash", pattern=SHA)

    @model_validator(mode="after")
    def exact_metrics(self) -> VersionSetTransport:
        if set(self.metric_definition_versions_by_metric_id) != {
            "ENTRY_WITHIN_WINDOW_AFTER_LOSS",
            "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS",
            "WIN_LOSS_HOLDING_DURATION_DIFFERENCE",
        }:
            raise ValueError("Exactly three Metric definitions are required.")
        return self


class AnalysisPeriodTransport(AnalyticsTransport):
    start_inclusive: datetime = Field(alias="startInclusive")
    end_exclusive: datetime = Field(alias="endExclusive")


class MetricMinimumsTransport(AnalyticsTransport):
    reentry_eligible_losses: int = Field(alias="reentryEligibleLosses", ge=1, strict=True)
    exposure_pairs: int = Field(alias="exposurePairs", ge=1, strict=True)
    prior_baseline_episodes: int = Field(alias="priorBaselineEpisodes", ge=1, strict=True)
    holding_wins: int = Field(alias="holdingWins", ge=1, strict=True)
    holding_losses: int = Field(alias="holdingLosses", ge=1, strict=True)


class FindingMinimumsTransport(AnalyticsTransport):
    reentry_eligible_losses: int = Field(alias="reentryEligibleLosses", ge=1, strict=True)
    exposure_pairs: int = Field(alias="exposurePairs", ge=1, strict=True)
    holding_wins: int = Field(alias="holdingWins", ge=1, strict=True)
    holding_losses: int = Field(alias="holdingLosses", ge=1, strict=True)


class AnalysisConfigTransport(AnalyticsTransport):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    analysis_period: AnalysisPeriodTransport = Field(alias="analysisPeriod")
    review_timezone: str = Field(alias="reviewTimezone", min_length=1)
    timezone_database_version: str = Field(alias="timezoneDatabaseVersion", min_length=1)
    outcome_basis: Literal["NET_TRADING_PNL"] = Field(alias="outcomeBasis")
    funding_treatment: Literal["EXCLUDED"] = Field(alias="fundingTreatment")
    eligible_review_unit_policy: Literal["COMPLETE_AND_RECONCILED_ONLY"] = Field(
        alias="eligibleReviewUnitPolicy"
    )
    reversal_treatment: Literal["SEPARATE"] = Field(alias="reversalTreatment")
    reentry_window: Literal["PT30M"] = Field(alias="reentryWindow")
    baseline_policy: Literal["PRIOR_PERIOD_SAME_INSTRUMENT_DIRECTION"] = Field(
        alias="baselinePolicy"
    )
    metric_minimums: MetricMinimumsTransport = Field(alias="metricMinimums")
    finding_minimums: FindingMinimumsTransport = Field(alias="findingMinimums")

    @model_validator(mode="after")
    def period(self) -> AnalysisConfigTransport:
        if self.analysis_period.start_inclusive >= self.analysis_period.end_exclusive:
            raise ValueError("Analysis period must be non-empty.")
        return self


class InputIdentityTransport(AnalyticsTransport):
    hash_specification_version: Literal["TRADING_ANALYTICS_INPUT_HASH_V1"] = Field(
        alias="hashSpecificationVersion"
    )
    ledger_revision_id: str = Field(alias="ledgerRevisionId", min_length=1)
    ledger_revision_content: ArtifactIdentityTransport = Field(alias="ledgerRevisionContent")
    reconciliation_manifest: ManifestIdentityTransport = Field(alias="reconciliationManifest")
    ledger_content_hash: str = Field(alias="ledgerContentHash", pattern=SHA)
    analysis_config_hash: str = Field(alias="analysisConfigHash", pattern=SHA)
    target_version_set: VersionSetTransport = Field(alias="targetVersionSet")
    capability_snapshot: list[Capability] = Field(alias="capabilitySnapshot")


class ResultDeliveryPolicyTransport(AnalyticsTransport):
    policy_version: Literal["ANALYTICS_RESULT_DELIVERY_V1"] = Field(alias="policyVersion")
    transport_mode: Literal["SINGLE_RESOURCE"] = Field(alias="transportMode")
    max_payload_bytes: int = Field(
        alias="maxPayloadBytes", ge=1, le=9_007_199_254_740_991, strict=True
    )


class CreateAnalyticsJobTransport(AnalyticsTransport):
    core_logical_job_id: str = Field(alias="coreLogicalJobId", min_length=1)
    core_analysis_run_id: str = Field(alias="coreAnalysisRunId", min_length=1)
    ledger_revision_id: str = Field(alias="ledgerRevisionId", min_length=1)
    ledger_revision_content: CorePayloadReferenceTransport = Field(alias="ledgerRevisionContent")
    reconciliation_manifest: CorePayloadReferenceTransport = Field(alias="reconciliationManifest")
    input_identity: InputIdentityTransport = Field(alias="inputIdentity")
    target_version_set: VersionSetTransport = Field(alias="targetVersionSet")
    analysis_config: AnalysisConfigTransport = Field(alias="analysisConfig")
    analysis_config_hash: str = Field(alias="analysisConfigHash", pattern=SHA)
    capability_snapshot: list[Capability] = Field(alias="capabilitySnapshot")
    expected_input_hash: str = Field(alias="expectedInputHash", pattern=SHA)
    input_hash_specification_version: Literal["TRADING_ANALYTICS_INPUT_HASH_V1"] = Field(
        alias="inputHashSpecificationVersion"
    )
    result_schema_version: Literal["TRADING_ANALYTICS_RESULT_SCHEMA_V1"] = Field(
        alias="resultSchemaVersion"
    )
    result_hash_specification_version: Literal["TRADING_ANALYTICS_RESULT_HASH_V1"] = Field(
        alias="resultHashSpecificationVersion"
    )
    retry_policy_version: Literal["ANALYTICS_RETRY_V1"] = Field(alias="retryPolicyVersion")
    result_delivery_policy: ResultDeliveryPolicyTransport = Field(alias="resultDeliveryPolicy")
    deletion_generation: int = Field(alias="deletionGeneration", ge=0, strict=True)
    attempt_token: str = Field(alias="attemptToken", min_length=1)


class UpdateAnalyticsGrantsTransport(AnalyticsTransport):
    mode: Literal["REPLACE_QUEUED_GRANTS", "START_RETRY_ATTEMPT"]
    expected_grant_revision: int = Field(alias="expectedGrantRevision", ge=0, strict=True)
    expected_attempt_token: str = Field(alias="expectedAttemptToken", min_length=1)
    next_attempt_token: str = Field(alias="nextAttemptToken", min_length=1)
    expected_input_hash: str = Field(alias="expectedInputHash", pattern=SHA)
    version_set_hash: str = Field(alias="versionSetHash", pattern=SHA)
    deletion_generation: int = Field(alias="deletionGeneration", ge=0, strict=True)
    expected_retry_cutoff_at: datetime = Field(alias="expectedRetryCutoffAt")
    ledger_revision_content: CorePayloadReferenceTransport = Field(alias="ledgerRevisionContent")
    reconciliation_manifest: CorePayloadReferenceTransport = Field(alias="reconciliationManifest")


class AcknowledgeAnalyticsTransport(AnalyticsTransport):
    core_analysis_run_id: str = Field(alias="coreAnalysisRunId", min_length=1)
    payload_id: str = Field(alias="payloadId", min_length=1)
    payload_sha256: str = Field(alias="payloadSha256", pattern=SHA)
    terminal_result_hash: str = Field(alias="terminalResultHash", pattern=SHA)
    attempt_token: str = Field(alias="attemptToken", min_length=1)
    deletion_generation: int = Field(alias="deletionGeneration", ge=0, strict=True)
    disposition: Literal["PERSISTED", "DISCARDED_STALE"]
    persisted_at: datetime | None = Field(default=None, alias="persistedAt")
    discarded_at: datetime | None = Field(default=None, alias="discardedAt")
    stale_reason: Literal["DELETED_GENERATION", "STALE_ATTEMPT"] | None = Field(
        default=None, alias="staleReason"
    )
    current_deletion_generation: int | None = Field(
        default=None, alias="currentDeletionGeneration", ge=1
    )
    current_attempt_token: str | None = Field(default=None, alias="currentAttemptToken")

    @model_validator(mode="after")
    def disposition_shape(self) -> AcknowledgeAnalyticsTransport:
        if self.disposition == "PERSISTED":
            if self.persisted_at is None or any(
                (
                    self.discarded_at,
                    self.stale_reason,
                    self.current_deletion_generation,
                    self.current_attempt_token,
                )
            ):
                raise ValueError("Invalid persisted acknowledgement.")
        elif self.discarded_at is None or self.stale_reason is None:
            raise ValueError("Invalid stale acknowledgement.")
        elif self.stale_reason == "DELETED_GENERATION":
            if self.current_deletion_generation is None or self.current_attempt_token is not None:
                raise ValueError("Deleted generation is required.")
        elif self.current_attempt_token is None or self.current_deletion_generation is not None:
            raise ValueError("Current attempt token is required.")
        return self
