"""Core-only Trading Review normalization job HTTP boundary."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from refinvest.api.schemas.trading_review_jobs import (
    AcknowledgeTradingReviewJobTransport,
    CreateTradingNormalizationJobTransport,
    TradingComputeJobAcceptedTransport,
    UpdateTradingNormalizationJobGrantsTransport,
)
from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.app.security import require_internal_api_key
from refinvest.app.trading_review_identity import (
    HASH_SPECIFICATION_V1,
    calculate_input_set_hash,
    supports_target_version_set,
)
from refinvest.application.trading_review_jobs import (
    TradingReviewCapacityExceeded,
    TradingReviewGrantInvalid,
    TradingReviewIdempotencyConflict,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
    hash_idempotency_key,
    submit_trading_review_job,
)
from refinvest.observability.metrics import ApiMetrics, safe_record
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.errors import TradingReviewError

router = APIRouter(tags=["trading-review-jobs"], dependencies=[Depends(require_internal_api_key)])
_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def get_trading_review_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PostgresTradingReviewJobRepository:
    if settings.database_url is None:
        raise HTTPException(status_code=503, detail="Job store is unavailable.")
    if settings.trading_review_active_job_limit is None:
        raise HTTPException(status_code=503, detail="Trading Review capacity is unavailable.")
    try:
        key = trading_review_payload_key(settings)
    except ValueError as error:
        raise HTTPException(
            status_code=503, detail="Terminal payload key is unavailable."
        ) from error
    return PostgresTradingReviewJobRepository(
        settings.database_url,
        key,
        active_job_limit=settings.trading_review_active_job_limit,
        grant_renewal_cutoff=timedelta(
            seconds=settings.trading_review_grant_renewal_cutoff_seconds
        ),
        grant_refresh_wait=timedelta(seconds=settings.trading_review_grant_refresh_wait_seconds),
        max_attempts=settings.trading_review_max_attempts,
    )


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


def _supported(request: CreateTradingNormalizationJobTransport) -> bool:
    return supports_target_version_set(
        request.input_identity.target_version_set,
        {
            item.instrument_id: item.instrument_terms_version
            for item in request.pinned_instrument_terms
        },
    )


def _semantic_request_error(body: CreateTradingNormalizationJobTransport) -> JSONResponse | None:
    if {item.role for item in body.artifact_access_grants} != {"TRADE_HISTORY", "POSITION_HISTORY"}:
        return _http_error(422, "FILE_FORMAT_INVALID", "Exactly one artifact per role is required.")
    if len({item.core_artifact_id for item in body.artifact_access_grants}) != 2:
        return _http_error(422, "FILE_FORMAT_INVALID", "Artifact identities must be distinct.")
    for artifact in body.artifact_access_grants:
        if (
            artifact.access_uri.username
            or artifact.access_uri.password
            or artifact.access_uri.fragment
        ):
            return _http_error(422, "ARTIFACT_ACCESS_DENIED", "Artifact grant URI is invalid.")
    for timezone_name in (body.input_identity.source_timezone, body.input_identity.review_timezone):
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return _http_error(422, "FILE_FORMAT_INVALID", "Timezone is unsupported.")
    if not body.pinned_instrument_terms or len(
        {item.instrument_id for item in body.pinned_instrument_terms}
    ) != len(body.pinned_instrument_terms):
        return _http_error(
            422, "INSTRUMENT_TERMS_UNAVAILABLE", "Pinned instrument terms are required."
        )
    for item in body.pinned_instrument_terms:
        if item.settlement_asset != body.input_identity.declared_product.settlement_asset:
            return _http_error(
                422, "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument settlement asset is invalid."
            )
        try:
            if any(
                ScaledDecimal.parse(value).value <= 0
                for value in (item.quantity_step, item.price_tick, item.contract_multiplier)
            ):
                return _http_error(
                    422, "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument grid is invalid."
                )
        except (ValueError, TradingReviewError):
            return _http_error(422, "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument grid is invalid.")
    return None


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
    repository: Annotated[
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TradingComputeJobAcceptedTransport | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    if body.input_identity.hash_specification_version == HASH_SPECIFICATION_V1 and not _supported(
        body
    ):
        return _http_error(422, "VERSION_INCOMPATIBLE", "Normalizer VersionSet is unsupported.")
    request_error = _semantic_request_error(body)
    if request_error is not None:
        return request_error
    try:
        computed_input_set_hash = (
            calculate_input_set_hash(body.input_identity)
            if body.input_identity.hash_specification_version == HASH_SPECIFICATION_V1
            else None
        )
        accepted = submit_trading_review_job(
            body.model_dump(mode="json", by_alias=True),
            key,
            repository,
            computed_input_set_hash,
        )
    except TradingReviewIdempotencyConflict:
        _record_submission(http_request, "conflict")
        return _http_error(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key conflicts with an existing request."
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
    repository: Annotated[
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    for artifact in body.artifact_access_grants:
        if (
            artifact.access_uri.username
            or artifact.access_uri.password
            or artifact.access_uri.fragment
        ):
            return _http_error(422, "ARTIFACT_ACCESS_DENIED", "Artifact grant URI is invalid.")
    try:
        receipt = repository.update_grants(
            jobId, hash_idempotency_key(key), body.model_dump(mode="json", by_alias=True)
        )
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
    repository: Annotated[
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
) -> dict[str, object] | JSONResponse:
    try:
        view = repository.get(jobId)
    except TradingReviewJobStateConflict:
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
    repository: Annotated[
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
) -> dict[str, object] | JSONResponse:
    try:
        view = repository.get(jobId)
    except TradingReviewJobStateConflict:
        return _http_error(409, "VERSION_INCOMPATIBLE", "Job identity is unavailable.")
    if view is None:
        return _http_error(404, "JOB_NOT_FOUND", "Job was not found.")
    if view.status is not TradingReviewJobStatus.COMPLETED:
        return _http_error(409, "RESULT_NOT_AVAILABLE", "Completed result is not available.")
    descriptor = repository.get_result_descriptor(jobId)
    if descriptor is None:
        return _http_error(409, "RESULT_NOT_AVAILABLE", "Completed result is not available.")
    return {
        "jobId": descriptor.job_id,
        "coreLogicalJobId": descriptor.core_logical_job_id,
        "payloadId": descriptor.payload_id,
        "mediaType": descriptor.media_type,
        "byteSize": descriptor.byte_size,
        "payloadSha256": descriptor.payload_sha256,
        "coreNormalizationRunId": descriptor.core_normalization_run_id,
        "inputSetHash": descriptor.input_set_hash,
        "hashSpecificationVersion": descriptor.hash_specification_version,
        "targetVersionSet": descriptor.target_version_set,
        "reconciliationManifestHash": descriptor.reconciliation_manifest_hash,
        "resultHash": descriptor.result_hash,
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
    repository: Annotated[
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    try:
        view = repository.request_cancellation(jobId, hash_idempotency_key(key))
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
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
) -> Response:
    payload = repository.get_terminal_payload(payloadId)
    if payload is None:
        return _http_error(404, "PAYLOAD_NOT_FOUND", "Payload was not found.")
    return Response(content=payload, media_type="application/json")


@router.post(
    "/trading-review/jobs/{jobId}/acknowledgement",
    operation_id="acknowledgeTradingReviewJobResult",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["trading-review-payloads"],
    response_model=None,
)
def acknowledge_trading_review_job_result(
    jobId: str,
    body: AcknowledgeTradingReviewJobTransport,
    repository: Annotated[
        PostgresTradingReviewJobRepository, Depends(get_trading_review_repository)
    ],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    key = _key(idempotency_key)
    if key is None:
        return _http_error(400, "REQUEST_INVALID", "Invalid Idempotency-Key.")
    recorded_at = body.persisted_at if body.disposition == "PERSISTED" else body.discarded_at
    if (
        recorded_at is None
        or (
            body.disposition == "PERSISTED"
            and (body.discarded_at is not None or body.current_deletion_generation is not None)
        )
        or (
            body.disposition == "DISCARDED_STALE"
            and (body.persisted_at is not None or body.current_deletion_generation is None)
        )
    ):
        return _http_error(422, "REQUEST_INVALID", "Acknowledgement shape is invalid.")
    try:
        accepted = repository.acknowledge(
            jobId,
            idempotency_key_hash=hash_idempotency_key(key),
            core_aggregate_id=body.core_aggregate_id,
            payload_id=body.payload_id,
            payload_sha256=body.payload_sha256,
            terminal_result_hash=body.terminal_result_hash,
            attempt_token=body.attempt_token,
            deletion_generation=body.deletion_generation,
            disposition=body.disposition,
            recorded_at=recorded_at,
            current_deletion_generation=body.current_deletion_generation,
        )
    except (TradingReviewIdempotencyConflict, TradingReviewJobStateConflict):
        return _http_error(
            409, "RESULT_HASH_MISMATCH", "Acknowledgement conflicts with the result."
        )
    if not accepted:
        return _http_error(404, "JOB_NOT_FOUND", "Job or payload was not found.")
    return Response(status_code=204)


def _record_submission(request: Request, outcome: str) -> None:
    metrics = getattr(request.app.state, "metrics", None)
    if isinstance(metrics, ApiMetrics):
        safe_record(metrics.trading_review_submissions.labels(outcome).inc)


def _record_grant_update(request: Request, outcome: str) -> None:
    metrics = getattr(request.app.state, "metrics", None)
    if isinstance(metrics, ApiMetrics):
        safe_record(metrics.trading_review_grant_updates.labels(outcome).inc)
