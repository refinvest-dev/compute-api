"""Core-only Trading Review normalization job HTTP boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine
from hmac import compare_digest
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from refinvest.api.dependencies.trading_analytics_jobs import get_optional_analytics_repository
from refinvest.api.dependencies.trading_review_jobs import get_trading_review_repository
from refinvest.api.schemas.trading_analytics_jobs import AcknowledgeAnalyticsTransport
from refinvest.api.schemas.trading_review_jobs import (
    AcknowledgeTradingReviewJobTransport,
    ArtifactReadGrantTransport,
    CreateTradingNormalizationJobTransport,
    TradingComputeJobAcceptedTransport,
    UpdateTradingNormalizationJobGrantsTransport,
)
from refinvest.api.security import require_internal_api_key
from refinvest.app.config import Settings, get_settings
from refinvest.application.analytics_jobs import (
    AnalyticsResultDeliveryRepository,
)
from refinvest.application.normalization_jobs import (
    NormalizationArtifactGrant,
    NormalizationInstrumentTerms,
    NormalizationJobIdentityUnavailable,
    NormalizationJobNotFound,
    NormalizationRequestInvalid,
    NormalizationResultUnavailable,
    SubmitNormalizationJob,
    UpdateNormalizationGrants,
    cancel_normalization_job,
    get_normalization_job_status,
    get_normalization_result,
    submit_normalization_job,
    update_normalization_grants,
)
from refinvest.application.terminal_delivery import (
    AnalyticsAcknowledgement,
    DeliveryFailureKind,
    NormalizationAcknowledgement,
    TerminalDeliveryFailure,
    acknowledge_terminal_result,
    get_terminal_payload,
)
from refinvest.application.trading_review_jobs import (
    NormalizationJobRepository,
    NormalizationResultDeliveryRepository,
    TradingReviewCapacityExceeded,
    TradingReviewGrantInvalid,
    TradingReviewIdempotencyConflict,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
)
from refinvest.observability.metrics import ApiMetrics, safe_record


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate JSON key.")
        values[key] = value
    return values


class _UniqueIdentityKeysRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def reject_duplicate_keys(request: Request) -> Response:
            if request.method == "POST" and request.url.path.startswith("/trading-review/"):
                settings_provider = cast(
                    Callable[[], Settings],
                    request.app.dependency_overrides.get(get_settings, get_settings),
                )
                configured_key = settings_provider().internal_api_key
                supplied_key = request.headers.get("X-Internal-Api-Key")
                if (
                    configured_key is not None
                    and supplied_key is not None
                    and compare_digest(configured_key, supplied_key)
                ):
                    try:
                        body = await request.body()
                        document: object = {}
                        if body:
                            document = json.loads(body, object_pairs_hook=_unique_json_object)
                    except (ValueError, UnicodeDecodeError):
                        return _http_error(422, "REQUEST_INVALID", "Malformed request body.")
                    if request.url.path == "/trading-review/normalization-jobs":
                        if not isinstance(document, dict):
                            return _http_error(422, "REQUEST_INVALID", "Malformed request body.")
                        version_request = cast(dict[str, object], document)
                        schema = version_request.get("resultSchemaVersion")
                        specification = version_request.get("resultHashSpecificationVersion")
                        if schema is None or specification is None:
                            return _http_error(
                                422, "REQUEST_INVALID", "Result version pin is required."
                            )
                        known_schema = {
                            "TRADING_NORMALIZATION_RESULT_V1",
                            "TRADING_NORMALIZATION_RESULT_V2",
                        }
                        known_specification = {
                            "TRADING_NORMALIZATION_RESULT_HASH_V1",
                            "TRADING_NORMALIZATION_RESULT_HASH_V2",
                        }
                        if (
                            not isinstance(schema, str)
                            or not isinstance(specification, str)
                            or schema not in known_schema
                            or specification not in known_specification
                        ):
                            return _http_error(
                                422, "RESULT_SCHEMA_UNSUPPORTED", "Result version is unsupported."
                            )
                        if schema[-2:] != specification[-2:]:
                            return _http_error(
                                422,
                                "RESULT_VERSION_PAIR_INVALID",
                                "Result version pair is invalid.",
                            )
            return await handler(request)

        return reject_duplicate_keys


router = APIRouter(
    tags=["trading-review-jobs"],
    dependencies=[Depends(require_internal_api_key)],
    route_class=_UniqueIdentityKeysRoute,
)
_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def _http_error(code: int, reason: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content={
            "code": reason,
            "message": message,
            "traceId": str(uuid4()),
            "retryable": retryable,
        },
    )


def _key(value: str | None) -> str | None:
    return value if value is not None and _KEY.fullmatch(value) else None


def _artifact_grants(
    grants: list[ArtifactReadGrantTransport],
) -> tuple[NormalizationArtifactGrant, ...]:
    return tuple(
        NormalizationArtifactGrant(
            grant.core_artifact_id,
            grant.role,
            bool(
                grant.access_uri.username or grant.access_uri.password or grant.access_uri.fragment
            ),
        )
        for grant in grants
    )


def _submission_command(body: CreateTradingNormalizationJobTransport) -> SubmitNormalizationJob:
    return SubmitNormalizationJob(
        payload=body.model_dump(mode="json", by_alias=True),
        identity=body.input_identity,
        artifacts=_artifact_grants(body.artifact_access_grants),
        terms=tuple(
            NormalizationInstrumentTerms(
                item.instrument_id,
                item.instrument_terms_version,
                item.settlement_asset,
                item.quantity_step,
                item.price_tick,
                item.contract_multiplier,
            )
            for item in body.pinned_instrument_terms
        ),
    )


def _status(view: TradingReviewJobStatusView) -> dict[str, object]:
    result: dict[str, object] = {
        "jobId": view.job_id,
        "coreLogicalJobId": view.core_logical_job_id,
        "kind": view.kind.value,
        "progressStage": view.progress_stage,
        "attempt": max(1, view.attempt),
        "createdAt": view.created_at.isoformat(),
        "updatedAt": view.updated_at.isoformat(),
        "startedAt": view.started_at.isoformat() if view.started_at else None,
        "status": view.status.value,
        "attemptToken": view.attempt_token,
        "deletionGeneration": view.deletion_generation,
        "grantRevision": view.grant_revision,
        "coreNormalizationRunId": view.core_normalization_run_id,
        "expectedInputSetHash": view.expected_input_set_hash,
        "hashSpecificationVersion": view.hash_specification_version,
        "targetVersionSet": view.target_version_set,
        "resultSchemaVersion": view.result_schema_version,
        "resultHashSpecificationVersion": view.result_hash_specification_version,
    }
    if view.terminal_at is not None:
        result["terminalAt"] = view.terminal_at.isoformat()
    else:
        result["grantAction"] = view.grant_action
        result["grantRefreshDeadlineAt"] = (
            view.grant_refresh_deadline_at.isoformat() if view.grant_refresh_deadline_at else None
        )
    if view.computed_input_set_hash is not None:
        result["inputSetHash"] = view.computed_input_set_hash
    if view.status is TradingReviewJobStatus.COMPLETED:
        result["terminalPayloadId"] = view.terminal_payload_id
        result["resultHash"] = view.result_hash
    if view.status is TradingReviewJobStatus.FAILED and view.failure is not None:
        result["failure"] = {
            "code": view.failure.code,
            "message": view.failure.message,
            "retryable": view.failure.retryable,
            "details": list(view.failure.details),
        }
    return result


@router.post(
    "/trading-review/normalization-jobs",
    operation_id="createTradingNormalizationJob",
    status_code=202,
    response_model=None,
)
def create_trading_normalization_job(
    body: CreateTradingNormalizationJobTransport,
    http_request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[NormalizationJobRepository, Depends(get_trading_review_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TradingComputeJobAcceptedTransport | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    try:
        accepted = submit_normalization_job(_submission_command(body), key, repository)
    except NormalizationRequestInvalid as error:
        return _http_error(422, error.code, str(error))
    except TradingReviewIdempotencyConflict:
        _record_submission(http_request, "conflict")
        return _http_error(
            409,
            "IDEMPOTENCY_PAYLOAD_CONFLICT",
            "Idempotency-Key conflicts with an existing request.",
        )
    except TradingReviewCapacityExceeded:
        _record_submission(http_request, "capacity")
        response = _http_error(
            503, "TRADING_REVIEW_CAPACITY_EXCEEDED", "Trading Review capacity is full.", True
        )
        response.headers["Retry-After"] = str(settings.trading_review_capacity_retry_after_seconds)
        return response
    except TradingReviewGrantInvalid:
        return _http_error(422, "ARTIFACT_REFERENCE_EXPIRED", "Artifact grant is invalid.")
    _record_submission(http_request, "accepted" if accepted.newly_created else "replay")
    return TradingComputeJobAcceptedTransport(
        jobId=accepted.job_id,
        coreLogicalJobId=accepted.core_logical_job_id,
        kind=accepted.kind.value,
        status="PENDING",
        createdAt=accepted.created_at,
        coreNormalizationRunId=accepted.core_normalization_run_id,
        resultSchemaVersion=accepted.result_schema_version,
        resultHashSpecificationVersion=accepted.result_hash_specification_version,
    )


@router.post(
    "/trading-review/normalization-jobs/{jobId}/grant-updates",
    operation_id="updateTradingNormalizationJobGrants",
    response_model=None,
)
def update_trading_normalization_job_grants(
    jobId: str,
    body: UpdateTradingNormalizationJobGrantsTransport,
    http_request: Request,
    repository: Annotated[NormalizationJobRepository, Depends(get_trading_review_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    try:
        receipt = update_normalization_grants(
            jobId,
            UpdateNormalizationGrants(
                body.model_dump(mode="json", by_alias=True),
                _artifact_grants(body.artifact_access_grants),
            ),
            key,
            repository,
        )
    except NormalizationRequestInvalid as error:
        return _http_error(422, error.code, str(error))
    except TradingReviewIdempotencyConflict:
        _record_grant_update(http_request, "conflict")
        return _http_error(409, "IDEMPOTENCY_CONFLICT", "Grant update key conflicts.")
    except TradingReviewGrantInvalid:
        return _http_error(422, "ARTIFACT_REFERENCE_EXPIRED", "Artifact grant is invalid.")
    except TradingReviewJobStateConflict:
        _record_grant_update(http_request, "conflict")
        return _http_error(409, "GRANT_UPDATE_CONFLICT", "Grant update conflicts with job state.")
    if receipt is None:
        return _http_error(404, "JOB_NOT_FOUND", "Job was not found.")
    _record_grant_update(http_request, "accepted")
    return {
        "jobId": receipt.job_id,
        "mode": receipt.mode,
        "grantRevision": receipt.grant_revision,
        "attemptToken": receipt.attempt_token,
        "status": receipt.status,
        "updatedAt": receipt.updated_at.isoformat(),
    }


@router.get(
    "/trading-review/normalization-jobs/{jobId}",
    operation_id="getTradingNormalizationJobStatus",
    response_model=None,
)
def get_trading_normalization_job_status(
    jobId: str,
    repository: Annotated[NormalizationJobRepository, Depends(get_trading_review_repository)],
) -> dict[str, object] | JSONResponse:
    try:
        view = get_normalization_job_status(jobId, repository)
    except NormalizationJobIdentityUnavailable:
        return _http_error(409, "VERSION_INCOMPATIBLE", "Job identity is unavailable.")
    return (
        _status(view)
        if view is not None
        else _http_error(404, "JOB_NOT_FOUND", "Job was not found.")
    )


@router.get(
    "/trading-review/normalization-jobs/{jobId}/result",
    operation_id="getTradingNormalizationJobResult",
    response_model=None,
)
def get_trading_normalization_job_result(
    jobId: str,
    repository: Annotated[NormalizationJobRepository, Depends(get_trading_review_repository)],
) -> dict[str, object] | JSONResponse:
    try:
        descriptor = get_normalization_result(jobId, repository)
    except NormalizationJobIdentityUnavailable:
        return _http_error(409, "VERSION_INCOMPATIBLE", "Job identity is unavailable.")
    except NormalizationJobNotFound:
        return _http_error(404, "JOB_NOT_FOUND", "Job was not found.")
    except NormalizationResultUnavailable:
        return _http_error(409, "RESULT_NOT_AVAILABLE", "Completed result is not available.")
    return {
        "jobId": descriptor.job_id,
        "coreLogicalJobId": descriptor.core_logical_job_id,
        "payloadId": descriptor.payload_id,
        "transportMode": "SINGLE_RESOURCE",
        "resultDeliveryPolicyVersion": descriptor.result_delivery_policy_version,
        "mediaType": descriptor.media_type,
        "contentEncoding": descriptor.content_encoding,
        "byteSize": descriptor.byte_size,
        "payloadSha256": descriptor.payload_sha256,
        "coreNormalizationRunId": descriptor.core_normalization_run_id,
        "inputSetHash": descriptor.input_set_hash,
        "hashSpecificationVersion": descriptor.hash_specification_version,
        "targetVersionSet": descriptor.target_version_set,
        "reconciliationManifestHash": descriptor.reconciliation_manifest_hash,
        "resultHash": descriptor.result_hash,
        "resultSchemaVersion": descriptor.result_schema_version,
        "resultHashSpecificationVersion": descriptor.result_hash_specification_version,
        "counts": descriptor.counts,
        "sectionDigests": descriptor.section_digests,
        "createdAt": descriptor.created_at.isoformat(),
        "expiresAt": descriptor.expires_at.isoformat(),
        "attemptToken": descriptor.attempt_token,
        "deletionGeneration": descriptor.deletion_generation,
    }


@router.post(
    "/trading-review/normalization-jobs/{jobId}/cancellation",
    operation_id="cancelTradingNormalizationJob",
    status_code=202,
    response_model=None,
)
def cancel_trading_normalization_job(
    jobId: str,
    repository: Annotated[NormalizationJobRepository, Depends(get_trading_review_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    try:
        view = cancel_normalization_job(jobId, key, repository)
    except TradingReviewIdempotencyConflict:
        return _http_error(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key conflicts with an existing request."
        )
    return (
        _status(view)
        if view is not None
        else _http_error(404, "JOB_NOT_FOUND", "Job was not found.")
    )


@router.get(
    "/trading-review/terminal-payloads/{payloadId}",
    operation_id="downloadTradingReviewTerminalPayload",
    tags=["trading-review-payloads"],
    response_model=None,
)
def download_trading_review_terminal_payload(
    payloadId: str,
    repository: Annotated[
        NormalizationResultDeliveryRepository, Depends(get_trading_review_repository)
    ],
    analytics_repository: Annotated[
        AnalyticsResultDeliveryRepository | None, Depends(get_optional_analytics_repository)
    ],
) -> Response:
    try:
        payload = get_terminal_payload(payloadId, repository, analytics_repository)
    except TerminalDeliveryFailure as error:
        return _delivery_error(error)
    return Response(
        content=payload.content,
        media_type=f"{payload.media_type}; charset=utf-8",
        headers={"Content-Encoding": "identity"},
    )


@router.post(
    "/trading-review/jobs/{jobId}/acknowledgement",
    operation_id="acknowledgeTradingReviewJobResult",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["trading-review-payloads"],
    response_model=None,
)
def acknowledge_trading_review_job_result(
    jobId: str,
    body: AcknowledgeTradingReviewJobTransport | AcknowledgeAnalyticsTransport,
    repository: Annotated[
        NormalizationResultDeliveryRepository, Depends(get_trading_review_repository)
    ],
    analytics_repository: Annotated[
        AnalyticsResultDeliveryRepository | None, Depends(get_optional_analytics_repository)
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    command: NormalizationAcknowledgement | AnalyticsAcknowledgement
    if isinstance(body, AcknowledgeAnalyticsTransport):
        command = AnalyticsAcknowledgement(
            body.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    else:
        command = NormalizationAcknowledgement(
            core_aggregate_id=body.core_aggregate_id,
            payload_id=body.payload_id,
            payload_sha256=body.payload_sha256,
            terminal_result_hash=body.terminal_result_hash,
            attempt_token=body.attempt_token,
            deletion_generation=body.deletion_generation,
            disposition=body.disposition,
            persisted_at=body.persisted_at,
            discarded_at=body.discarded_at,
            current_deletion_generation=body.current_deletion_generation,
        )
    try:
        acknowledge_terminal_result(jobId, command, key, repository, analytics_repository)
    except TerminalDeliveryFailure as error:
        return _delivery_error(error)
    return Response(status_code=204)


def _delivery_error(error: TerminalDeliveryFailure) -> JSONResponse:
    status_code = {
        DeliveryFailureKind.NOT_FOUND: 404,
        DeliveryFailureKind.CONFLICT: 409,
        DeliveryFailureKind.INVALID: 422,
        DeliveryFailureKind.UNAVAILABLE: 503,
    }[error.kind]
    return _http_error(status_code, error.code, str(error), error.retryable)


def _record_submission(request: Request, outcome: str) -> None:
    metrics = getattr(request.app.state, "metrics", None)
    if isinstance(metrics, ApiMetrics):
        safe_record(metrics.trading_review_submissions.labels(outcome).inc)


def _record_grant_update(request: Request, outcome: str) -> None:
    metrics = getattr(request.app.state, "metrics", None)
    if isinstance(metrics, ApiMetrics):
        safe_record(metrics.trading_review_grant_updates.labels(outcome).inc)
