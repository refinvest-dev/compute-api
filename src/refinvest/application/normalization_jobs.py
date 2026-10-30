"""Normalization admission policy and job commands, independent of HTTP and SQL."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from refinvest.application.trading_review_jobs import (
    GrantUpdateReceipt,
    NormalizationJobRepository,
    TerminalPayloadDescriptor,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
    TradingReviewJobSubmission,
    hash_idempotency_key,
    submit_trading_review_job,
)
from refinvest.contracts.normalization.identity import (
    HASH_SPECIFICATION_V1,
    NormalizationInputIdentity,
    calculate_input_set_hash,
    supports_target_version_set,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.errors import TradingReviewError


@dataclass(frozen=True, slots=True)
class NormalizationArtifactGrant:
    core_artifact_id: str
    role: str
    # URI syntax/decoding belongs to the inbound adapter; this is its parsed fact.
    has_credentials_or_fragment: bool


@dataclass(frozen=True, slots=True)
class NormalizationInstrumentTerms:
    instrument_id: str
    instrument_terms_version: str
    settlement_asset: str
    quantity_step: str
    price_tick: str
    contract_multiplier: str


@dataclass(frozen=True, slots=True)
class SubmitNormalizationJob:
    # Reuse the one transport projection retained by the durable job; do not copy it again.
    payload: dict[str, object]
    identity: NormalizationInputIdentity
    artifacts: tuple[NormalizationArtifactGrant, ...]
    terms: tuple[NormalizationInstrumentTerms, ...]


@dataclass(frozen=True, slots=True)
class UpdateNormalizationGrants:
    payload: dict[str, object]
    artifacts: tuple[NormalizationArtifactGrant, ...]


class NormalizationRequestInvalid(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NormalizationJobNotFound(ValueError):
    """The requested job does not exist."""


class NormalizationJobIdentityUnavailable(ValueError):
    """A persisted job cannot provide its required identity pins."""


class NormalizationResultUnavailable(ValueError):
    """The job is not completed, or its terminal descriptor is no longer available."""


def submit_normalization_job(
    command: SubmitNormalizationJob,
    idempotency_key: str,
    repository: NormalizationJobRepository,
) -> TradingReviewJobSubmission:
    """Preserve version → artifact → timezone → terms → hash → atomic admission order."""
    identity = command.identity
    if (
        identity.hash_specification_version == HASH_SPECIFICATION_V1
        and not supports_target_version_set(
            identity.target_version_set,
            {item.instrument_id: item.instrument_terms_version for item in command.terms},
        )
    ):
        raise NormalizationRequestInvalid(
            "VERSION_INCOMPATIBLE", "Normalizer VersionSet is unsupported."
        )
    if {item.role for item in command.artifacts} != {"TRADE_HISTORY", "POSITION_HISTORY"}:
        raise NormalizationRequestInvalid(
            "FILE_FORMAT_INVALID", "Exactly one artifact per role is required."
        )
    if len({item.core_artifact_id for item in command.artifacts}) != 2:
        raise NormalizationRequestInvalid(
            "FILE_FORMAT_INVALID", "Artifact identities must be distinct."
        )
    _validate_artifact_locations(command.artifacts)
    for timezone_name in (identity.source_timezone, identity.review_timezone):
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise NormalizationRequestInvalid(
                "FILE_FORMAT_INVALID", "Timezone is unsupported."
            ) from error
    if not command.terms or len({item.instrument_id for item in command.terms}) != len(
        command.terms
    ):
        raise NormalizationRequestInvalid(
            "INSTRUMENT_TERMS_UNAVAILABLE", "Pinned instrument terms are required."
        )
    for item in command.terms:
        if item.settlement_asset != identity.declared_product.settlement_asset:
            raise NormalizationRequestInvalid(
                "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument settlement asset is invalid."
            )
        try:
            invalid_grid = any(
                ScaledDecimal.parse(value).value <= 0
                for value in (item.quantity_step, item.price_tick, item.contract_multiplier)
            )
        except (ValueError, TradingReviewError) as error:
            raise NormalizationRequestInvalid(
                "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument grid is invalid."
            ) from error
        if invalid_grid:
            raise NormalizationRequestInvalid(
                "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument grid is invalid."
            )
    computed_hash = (
        calculate_input_set_hash(identity)
        if identity.hash_specification_version == HASH_SPECIFICATION_V1
        else None
    )
    return submit_trading_review_job(command.payload, idempotency_key, repository, computed_hash)


def get_normalization_job_status(
    job_id: str, repository: NormalizationJobRepository
) -> TradingReviewJobStatusView | None:
    try:
        return repository.get(job_id)
    except TradingReviewJobStateConflict as error:
        raise NormalizationJobIdentityUnavailable("Job identity is unavailable.") from error


def update_normalization_grants(
    job_id: str,
    command: UpdateNormalizationGrants,
    idempotency_key: str,
    repository: NormalizationJobRepository,
) -> GrantUpdateReceipt | None:
    _validate_artifact_locations(command.artifacts)
    return repository.update_grants(job_id, hash_idempotency_key(idempotency_key), command.payload)


def cancel_normalization_job(
    job_id: str, idempotency_key: str, repository: NormalizationJobRepository
) -> TradingReviewJobStatusView | None:
    return repository.request_cancellation(job_id, hash_idempotency_key(idempotency_key))


def get_normalization_result(
    job_id: str, repository: NormalizationJobRepository
) -> TerminalPayloadDescriptor:
    view = get_normalization_job_status(job_id, repository)
    if view is None:
        raise NormalizationJobNotFound("Job was not found.")
    if view.status is not TradingReviewJobStatus.COMPLETED:
        raise NormalizationResultUnavailable("Completed result is not available.")
    descriptor = repository.get_result_descriptor(job_id)
    if descriptor is None:
        raise NormalizationResultUnavailable("Completed result is not available.")
    return descriptor


def _validate_artifact_locations(artifacts: tuple[NormalizationArtifactGrant, ...]) -> None:
    if any(item.has_credentials_or_fragment for item in artifacts):
        raise NormalizationRequestInvalid(
            "ARTIFACT_ACCESS_DENIED", "Artifact grant URI is invalid."
        )
