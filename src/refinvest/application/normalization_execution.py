"""Pinned Normalization input preparation, calculation and public-result projection."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from refinvest.application.normalization_payload import build_normalization_payload
from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
)
from refinvest.contracts.normalization.identity import (
    HASH_SPECIFICATION_V1,
    NormalizationInputIdentity,
    calculate_input_set_hash,
    supports_target_version_set,
)
from refinvest.contracts.normalization.result_delivery import MEDIA_TYPE, effective_payload_limit
from refinvest.contracts.normalization.result_integrity import (
    ResultIntegrityError,
    canonicalize_result,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.errors import TradingReviewError
from refinvest.trading_review.model import (
    InstrumentDescriptor,
    NormalizationRequest,
    NormalizationVersionSet,
    SourceArtifact,
    SourceArtifactRole,
)
from refinvest.trading_review.service import normalize_and_reconstruct
from refinvest.trading_review.versions import SUPPORTED_VERSION_SET

_LOGGER = logging.getLogger("refinvest.app.trading_review_normalization")


@dataclass(frozen=True, slots=True)
class NormalizationExecutionSettings:
    trading_review_timezone_database_version: str | None
    trading_review_identity_key: str | None
    trading_review_masking_key: str | None
    trading_review_artifact_hosts: tuple[str, ...]
    trading_review_max_payload_bytes: int


@dataclass(frozen=True, slots=True)
class CompletedNormalization:
    payload: bytes
    media_type: str
    payload_sha256: str
    input_set_hash: str
    reconciliation_manifest_hash: str
    result_hash: str


@dataclass(frozen=True, slots=True)
class FailedNormalization:
    failure: SafeTradingReviewFailure
    retryable: bool


NormalizationOutcome = CompletedNormalization | FailedNormalization
NormalizationArtifactDownloader = Callable[[dict[str, object], tuple[str, ...]], bytes]


class NormalizationExecutor(Protocol):
    def execute(
        self, claim: ClaimedTradingReviewJob, cancellation_check: Callable[[], bool]
    ) -> NormalizationOutcome: ...


def execute_normalization(
    settings: NormalizationExecutionSettings,
    claim: ClaimedTradingReviewJob,
    download_artifact: NormalizationArtifactDownloader,
) -> NormalizationOutcome:
    """Verify pinned input and calculate; the execution boundary maps failures and owns IPC."""
    request = claim.request_payload
    if (
        request.get("resultSchemaVersion") != claim.result_schema_version
        or request.get("resultHashSpecificationVersion") != claim.result_hash_specification_version
    ):
        raise ResultIntegrityError("RESULT_VERSION_PIN_MISMATCH")
    identity = NormalizationInputIdentity.model_validate(request["inputIdentity"])
    if identity.hash_specification_version != HASH_SPECIFICATION_V1:
        raise execution_failure(
            "INPUT_HASH_SPECIFICATION_UNSUPPORTED", "VALIDATION", "HASH_SPECIFICATION_UNSUPPORTED"
        )
    input_set_hash = calculate_input_set_hash(identity)
    if (
        input_set_hash != claim.computed_input_set_hash
        or input_set_hash != claim.expected_input_set_hash
    ):
        raise execution_failure("INPUT_SET_HASH_MISMATCH", "VALIDATION", "INPUT_IDENTITY_MISMATCH")
    if not settings.trading_review_timezone_database_version:
        raise _configuration_error("REFINVEST_TRADING_REVIEW_TZDB_VERSION")
    identity_key = _secret_key(
        settings.trading_review_identity_key, "REFINVEST_TRADING_IDENTITY_KEY"
    )
    masking_key = _secret_key(settings.trading_review_masking_key, "REFINVEST_TRADING_MASKING_KEY")
    artifacts_node = request.get("artifactAccessGrants")
    if not isinstance(artifacts_node, list) or len(cast(list[object], artifacts_node)) != 2:
        return FailedNormalization(
            SafeTradingReviewFailure(
                "FILE_FORMAT_INVALID",
                "Exactly one artifact per role is required.",
                False,
                "VALIDATION",
            ),
            False,
        )
    grants = cast(list[object], artifacts_node)
    artifacts: list[SourceArtifact] = []
    downloaded_by_role: dict[str, SourceArtifact] = {}
    for raw_grant in grants:
        grant = cast(dict[str, object], raw_grant) if isinstance(raw_grant, dict) else None
        if grant is None:
            raise execution_failure(
                "FILE_FORMAT_INVALID", "VALIDATION", "ARTIFACT_DESCRIPTOR_INVALID"
            )
        role_value = str(grant.get("role", ""))
        if role_value not in {role.value for role in SourceArtifactRole}:
            raise execution_failure("FILE_FORMAT_INVALID", "VALIDATION", "ARTIFACT_ROLE_INVALID")
        role = SourceArtifactRole(role_value)
        if role_value in downloaded_by_role:
            raise execution_failure("FILE_FORMAT_INVALID", "VALIDATION", "DUPLICATE_ARTIFACT_ROLE")
        declared_artifact = identity.artifacts_by_role[
            cast(Literal["TRADE_HISTORY", "POSITION_HISTORY"], role_value)
        ]
        if (
            grant.get("coreArtifactId") != declared_artifact.artifact_id
            or grant.get("rawSha256") != declared_artifact.sha256
        ):
            raise execution_failure(
                "ARTIFACT_IDENTITY_CONFLICT", "VALIDATION", "ARTIFACT_GRANT_IDENTITY_MISMATCH"
            )
        content = download_artifact(grant, settings.trading_review_artifact_hosts)
        artifact = SourceArtifact(
            artifact_id=str(grant["coreArtifactId"]),
            role=role,
            dialect=_dialect_for_role(identity, role),
            content=content,
        )
        artifacts.append(artifact)
        downloaded_by_role[role_value] = artifact
    if set(downloaded_by_role) != {role.value for role in SourceArtifactRole}:
        raise execution_failure(
            "FILE_FORMAT_INVALID", "VALIDATION", "REQUIRED_ARTIFACT_ROLE_MISSING"
        )
    terms_node = request.get("pinnedInstrumentTerms")
    if not isinstance(terms_node, list) or not terms_node:
        raise execution_failure(
            "INSTRUMENT_TERMS_UNAVAILABLE", "VALIDATION", "PINNED_TERMS_REQUIRED"
        )
    term_nodes = cast(list[object], terms_node)
    instruments = tuple(
        _instrument(cast(dict[str, object], item)) for item in term_nodes if isinstance(item, dict)
    )
    if len(instruments) != len(term_nodes):
        raise execution_failure(
            "INSTRUMENT_TERMS_UNAVAILABLE", "VALIDATION", "PINNED_TERMS_INVALID"
        )
    version_set = _version_set(identity, instruments)
    engine_request = NormalizationRequest(
        artifacts=tuple(artifacts),
        source_timezone=identity.source_timezone,
        timezone_database_version=settings.trading_review_timezone_database_version,
        declared_position_mode="ONE_WAY",
        instruments=instruments,
        version_set=version_set,
        stable_identity_key=identity_key,
    )
    result = normalize_and_reconstruct(engine_request)
    terminal_at = datetime.now(UTC)
    payload_object = build_normalization_payload(
        result,
        job_id=claim.job_id,
        core_logical_job_id=claim.core_logical_job_id,
        core_import_session_id=claim.core_import_session_id,
        core_normalization_run_id=claim.core_normalization_run_id,
        attempt_token=claim.attempt_token,
        deletion_generation=claim.deletion_generation,
        input_set_hash=input_set_hash,
        hash_specification_version=identity.hash_specification_version,
        target_version_set=identity.target_version_set.model_dump(mode="json", by_alias=True),
        artifacts=tuple(artifacts),
        masking_key=masking_key,
        created_at=terminal_at,
        terminal_at=terminal_at,
        instrument_terms=instruments,
        result_schema_version=claim.result_schema_version,
        result_hash_specification_version=claim.result_hash_specification_version,
    )
    try:
        payload = canonicalize_result(payload_object)
    except ResultIntegrityError:
        raise
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise execution_failure(
            "INTERNAL_ERROR", "PUBLICATION", "PAYLOAD_SERIALIZATION_FAILED"
        ) from None
    policy = cast(dict[str, object], request["resultDeliveryPolicy"])
    limit, limited_by = effective_payload_limit(policy, settings.trading_review_max_payload_bytes)
    _LOGGER.info(
        "trading_review_result_delivery_limit policy_version=%s core_maximum=%d "
        "compute_maximum=%d effective_maximum=%d limited_by=%s",
        policy["policyVersion"],
        policy["maxAcceptedPayloadBytes"],
        settings.trading_review_max_payload_bytes,
        limit,
        limited_by,
    )
    if not payload:
        raise execution_failure("INCOMPLETE_ANALYSIS_RESULT", "PUBLICATION", "PAYLOAD_EMPTY")
    if len(payload) > limit:
        raise execution_failure(
            "RESULT_PAYLOAD_LIMIT_EXCEEDED", "PUBLICATION", "PAYLOAD_LIMIT_EXCEEDED"
        )
    return CompletedNormalization(
        payload,
        MEDIA_TYPE,
        hashlib.sha256(payload).hexdigest(),
        input_set_hash,
        str(cast(dict[str, object], payload_object["reconciliationManifest"])["manifestHash"]),
        str(payload_object["resultHash"]),
    )


def normalization_failure(error: Exception) -> FailedNormalization:
    if isinstance(error, TradingReviewError):
        detail = error.detail
        safe_details: tuple[dict[str, object], ...] = ()
        if detail is not None:
            item: dict[str, object] = {}
            if detail.field is not None:
                item["field"] = detail.field
            if detail.reason is not None:
                item["reason"] = detail.reason
            if detail.row_number is not None:
                item["rowNumber"] = detail.row_number
            if detail.artifact_role is not None:
                item["artifactRole"] = detail.artifact_role
            safe_details = (item,)
        failure = SafeTradingReviewFailure(
            code=error.code.value,
            message="Normalization input failed a supported validation or reconstruction rule.",
            retryable=False,
            stage=error.stage.value,
            details=safe_details,
        )
        return FailedNormalization(failure, False)
    if isinstance(error, ResultIntegrityError):
        return FailedNormalization(
            SafeTradingReviewFailure(
                error.code, "Normalization result integrity failed.", False, "PUBLICATION"
            ),
            False,
        )
    if isinstance(error, NormalizationExecutionError):
        return FailedNormalization(error.failure, error.retryable)
    _LOGGER.warning("trading_review_normalization_failed")
    failure = SafeTradingReviewFailure(
        "INTERNAL_ERROR", "Normalization could not be completed.", True, "EXECUTION"
    )
    return FailedNormalization(failure, True)


class NormalizationExecutionError(Exception):
    def __init__(self, failure: SafeTradingReviewFailure, retryable: bool) -> None:
        self.failure = failure
        self.retryable = retryable


def _version_set(
    identity: NormalizationInputIdentity, instruments: tuple[InstrumentDescriptor, ...]
) -> NormalizationVersionSet:
    terms = {item.instrument_id: item.instrument_terms_version for item in instruments}
    if not supports_target_version_set(identity.target_version_set, terms):
        raise execution_failure("VERSION_INCOMPATIBLE", "VALIDATION", "VERSION_SET_INVALID")
    return SUPPORTED_VERSION_SET


def _instrument(node: dict[str, object]) -> InstrumentDescriptor:
    try:
        return InstrumentDescriptor(
            instrument_id=str(node["instrumentId"]),
            venue_symbol=str(node["venueSymbol"]),
            settlement_asset=str(node["settlementAsset"]),
            quantity_step=ScaledDecimal.parse(str(node["quantityStep"])),
            price_tick=ScaledDecimal.parse(str(node["priceTick"])),
            contract_multiplier=ScaledDecimal.parse(str(node["contractMultiplier"])),
            settlement_precision=int(str(node["settlementPrecision"])),
            instrument_terms_version=str(node["instrumentTermsVersion"]),
        )
    except (KeyError, ValueError, TypeError):
        raise execution_failure(
            "INSTRUMENT_TERMS_UNAVAILABLE", "VALIDATION", "PINNED_TERMS_INVALID"
        ) from None


def _dialect_for_role(identity: NormalizationInputIdentity, role: SourceArtifactRole) -> str:
    value = identity.target_version_set.normalizer.source_dialect_versions_by_artifact_role.get(
        role.value
    )
    if not value:
        raise execution_failure(
            "VERSION_INCOMPATIBLE", "VALIDATION", "ROLE_DIALECT_VERSION_MISSING"
        )
    return value


def _secret_key(value: str | None, name: str) -> bytes:
    if value is None or len(value.encode("utf-8")) < 32:
        raise _configuration_error(name)
    return value.encode("utf-8")


def _configuration_error(name: str) -> NormalizationExecutionError:
    return execution_failure("VERSION_UNAVAILABLE", "CONFIGURATION", f"{name}_NOT_CONFIGURED")


def execution_failure(
    code: str, stage: str, reason: str, retryable: bool = False
) -> NormalizationExecutionError:
    return NormalizationExecutionError(
        SafeTradingReviewFailure(
            code,
            "Normalization input or runtime dependency could not be processed.",
            retryable,
            stage,
            ({"field": "artifact", "reason": reason},) if stage == "ARTIFACT_FETCH" else (),
        ),
        retryable,
    )
