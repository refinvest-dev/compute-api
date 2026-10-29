"""Core-only HTTP operations for the distinct Analytics Job Protocol V1."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine
from hmac import compare_digest
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from refinvest.api.schemas.trading_analytics_jobs import (
    CreateAnalyticsJobTransport,
    UpdateAnalyticsGrantsTransport,
)
from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.app.security import require_internal_api_key
from refinvest.application.trading_review_jobs import hash_idempotency_key
from refinvest.contracts.analytics.identity import (
    AnalyticsContractError,
    create_fingerprint,
    require_supported_version_set,
    validate_create_identity,
)
from refinvest.persistence.postgres.trading_analytics_jobs import (
    AnalyticsJobCapacity,
    AnalyticsJobConflict,
    AnalyticsJobUnavailable,
    PostgresAnalyticsJobRepository,
)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key.")
        value[key] = item
    return value


class _UniqueAnalyticsKeysRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def reject_duplicate_keys(request: Request) -> Response:
            if request.method == "POST":
                provider = cast(
                    Callable[[], Settings],
                    request.app.dependency_overrides.get(get_settings, get_settings),
                )
                configured_key = provider().internal_api_key
                supplied_key = request.headers.get("X-Internal-Api-Key")
                if (
                    configured_key is not None
                    and supplied_key is not None
                    and compare_digest(configured_key, supplied_key)
                ):
                    try:
                        body = await request.body()
                        if body:
                            json.loads(body, object_pairs_hook=_unique_json_object)
                    except (ValueError, UnicodeDecodeError):
                        return _error(422, "REQUEST_INVALID")
            return await handler(request)

        return reject_duplicate_keys


router = APIRouter(
    tags=["trading-review-jobs"],
    dependencies=[Depends(require_internal_api_key)],
    route_class=_UniqueAnalyticsKeysRoute,
)
_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def _error(status_code: int, code: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": "Analytics request could not be completed.",
            "traceId": str(uuid4()),
            "retryable": retryable,
        },
    )


def _key(value: str | None) -> str | None:
    return value if value is not None and _KEY.fullmatch(value) else None


def get_optional_analytics_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PostgresAnalyticsJobRepository | None:
    if settings.database_url is None or settings.trading_review_active_job_limit is None:
        return None
    try:
        key = trading_review_payload_key(settings)
    except ValueError:
        return None
    return PostgresAnalyticsJobRepository(
        settings.database_url,
        key,
        active_job_limit=settings.trading_review_active_job_limit,
        max_payload_bytes=settings.trading_review_max_payload_bytes,
    )


def get_analytics_repository(
    repository: Annotated[
        PostgresAnalyticsJobRepository | None, Depends(get_optional_analytics_repository)
    ],
) -> PostgresAnalyticsJobRepository:
    if repository is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="Analytics job store is unavailable.")
    return repository


@router.post(
    "/trading-review/analytics-jobs",
    operation_id="createTradingAnalyticsJob",
    response_model=None,
)
def create_trading_analytics_job(
    body: CreateAnalyticsJobTransport,
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[PostgresAnalyticsJobRepository, Depends(get_analytics_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _error(400, "IDEMPOTENCY_KEY_INVALID")
    request = body.model_dump(mode="json", by_alias=True)
    if any(
        reference.byte_size > settings.trading_analytics_max_input_bytes
        for reference in (body.ledger_revision_content, body.reconciliation_manifest)
    ):
        return _error(503, "TEMPORARY_DEPENDENCY_FAILURE", retryable=True)
    try:
        validate_create_identity(request)
        implementation_digest = settings.trading_analytics_implementation_digest
        if implementation_digest is None:
            return _error(503, "TEMPORARY_DEPENDENCY_FAILURE", retryable=True)
        require_supported_version_set(request["targetVersionSet"], implementation_digest)
        response, created = repository.create(
            request, hash_idempotency_key(key), create_fingerprint(request)
        )
    except AnalyticsContractError as error:
        return _error(422, error.code)
    except AnalyticsJobCapacity:
        result = _error(503, "TRADING_REVIEW_CAPACITY_EXCEEDED", retryable=True)
        result.headers["Retry-After"] = str(settings.trading_review_capacity_retry_after_seconds)
        return result
    except AnalyticsJobConflict as error:
        return _error(409, str(error))
    return JSONResponse(status_code=202 if created else 200, content=response)


@router.get(
    "/trading-review/analytics-jobs/{jobId}",
    operation_id="getTradingAnalyticsJobStatus",
    response_model=None,
)
def get_trading_analytics_job_status(
    jobId: str,
    repository: Annotated[PostgresAnalyticsJobRepository, Depends(get_analytics_repository)],
) -> dict[str, object] | JSONResponse:
    result = repository.get(jobId)
    return result if result is not None else _error(404, "JOB_NOT_FOUND")


@router.get(
    "/trading-review/analytics-jobs/{jobId}/result",
    operation_id="getTradingAnalyticsJobResult",
    response_model=None,
)
def get_trading_analytics_job_result(
    jobId: str,
    repository: Annotated[PostgresAnalyticsJobRepository, Depends(get_analytics_repository)],
) -> dict[str, object] | JSONResponse:
    job = repository.get(jobId)
    if job is None:
        return _error(404, "JOB_NOT_FOUND")
    if job["status"] != "COMPLETED":
        return _error(409, "RESULT_NOT_AVAILABLE")
    try:
        result = repository.result_descriptor(jobId)
    except AnalyticsJobUnavailable:
        return _error(404, "RESULT_PAYLOAD_EXPIRED")
    return result if result is not None else _error(409, "RESULT_NOT_AVAILABLE")


@router.post(
    "/trading-review/analytics-jobs/{jobId}/cancellation",
    operation_id="cancelTradingAnalyticsJob",
    status_code=202,
    response_model=None,
)
def cancel_trading_analytics_job(
    jobId: str,
    repository: Annotated[PostgresAnalyticsJobRepository, Depends(get_analytics_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _error(400, "IDEMPOTENCY_KEY_INVALID")
    try:
        repository.cancel(jobId, hash_idempotency_key(key))
    except AnalyticsJobUnavailable:
        return _error(404, "JOB_NOT_FOUND")
    except AnalyticsJobConflict:
        return _error(409, "JOB_STATE_CONFLICT")
    return repository.get(jobId) or _error(404, "JOB_NOT_FOUND")


@router.post(
    "/trading-review/analytics-jobs/{jobId}/input-grants",
    operation_id="updateTradingAnalyticsJobInputGrants",
    response_model=None,
)
def update_trading_analytics_job_input_grants(
    jobId: str,
    body: UpdateAnalyticsGrantsTransport,
    repository: Annotated[PostgresAnalyticsJobRepository, Depends(get_analytics_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _error(400, "IDEMPOTENCY_KEY_INVALID")
    try:
        return repository.update_grants(
            jobId, body.model_dump(mode="json", by_alias=True), hash_idempotency_key(key)
        )
    except AnalyticsJobUnavailable:
        return _error(404, "JOB_NOT_FOUND")
    except AnalyticsJobConflict as error:
        return _error(409, str(error))
