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

from refinvest.api.dependencies.trading_analytics_jobs import (
    get_analytics_repository,
)
from refinvest.api.schemas.trading_analytics_jobs import (
    CreateAnalyticsJobTransport,
    UpdateAnalyticsGrantsTransport,
)
from refinvest.app.config import Settings, get_settings
from refinvest.app.security import require_internal_api_key
from refinvest.application.analytics_jobs import (
    AnalyticsAdmissionUnavailable,
    AnalyticsCancellationConflict,
    AnalyticsJobCapacity,
    AnalyticsJobConflict,
    AnalyticsJobNotFound,
    AnalyticsJobRepository,
    AnalyticsJobUnavailable,
    AnalyticsResultExpired,
    AnalyticsResultUnavailable,
    cancel_analytics_job,
    get_analytics_job_status,
    get_analytics_result,
    submit_analytics_job,
    update_analytics_grants,
)
from refinvest.contracts.analytics.identity import AnalyticsContractError


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


@router.post(
    "/trading-review/analytics-jobs",
    operation_id="createTradingAnalyticsJob",
    response_model=None,
)
def create_trading_analytics_job(
    body: CreateAnalyticsJobTransport,
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[AnalyticsJobRepository, Depends(get_analytics_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _error(400, "IDEMPOTENCY_KEY_INVALID")
    try:
        response, created = submit_analytics_job(
            body.model_dump(mode="json", by_alias=True),
            key,
            repository,
            max_input_bytes=settings.trading_analytics_max_input_bytes,
            implementation_digest=settings.trading_analytics_implementation_digest,
        )
    except AnalyticsAdmissionUnavailable:
        return _error(503, "TEMPORARY_DEPENDENCY_FAILURE", retryable=True)
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
    repository: Annotated[AnalyticsJobRepository, Depends(get_analytics_repository)],
) -> dict[str, object] | JSONResponse:
    result = get_analytics_job_status(jobId, repository)
    return result if result is not None else _error(404, "JOB_NOT_FOUND")


@router.get(
    "/trading-review/analytics-jobs/{jobId}/result",
    operation_id="getTradingAnalyticsJobResult",
    response_model=None,
)
def get_trading_analytics_job_result(
    jobId: str,
    repository: Annotated[AnalyticsJobRepository, Depends(get_analytics_repository)],
) -> dict[str, object] | JSONResponse:
    try:
        return get_analytics_result(jobId, repository)
    except AnalyticsJobNotFound:
        return _error(404, "JOB_NOT_FOUND")
    except AnalyticsResultUnavailable:
        return _error(409, "RESULT_NOT_AVAILABLE")
    except AnalyticsResultExpired:
        return _error(404, "RESULT_PAYLOAD_EXPIRED")


@router.post(
    "/trading-review/analytics-jobs/{jobId}/cancellation",
    operation_id="cancelTradingAnalyticsJob",
    status_code=202,
    response_model=None,
)
def cancel_trading_analytics_job(
    jobId: str,
    repository: Annotated[AnalyticsJobRepository, Depends(get_analytics_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _error(400, "IDEMPOTENCY_KEY_INVALID")
    try:
        result = cancel_analytics_job(jobId, key, repository)
    except AnalyticsJobNotFound:
        return _error(404, "JOB_NOT_FOUND")
    except AnalyticsCancellationConflict:
        return _error(409, "JOB_STATE_CONFLICT")
    return result or _error(404, "JOB_NOT_FOUND")


@router.post(
    "/trading-review/analytics-jobs/{jobId}/input-grants",
    operation_id="updateTradingAnalyticsJobInputGrants",
    response_model=None,
)
def update_trading_analytics_job_input_grants(
    jobId: str,
    body: UpdateAnalyticsGrantsTransport,
    repository: Annotated[AnalyticsJobRepository, Depends(get_analytics_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object] | JSONResponse:
    key = _key(idempotency_key)
    if key is None:
        return _error(400, "IDEMPOTENCY_KEY_INVALID")
    try:
        return update_analytics_grants(
            jobId, body.model_dump(mode="json", by_alias=True), key, repository
        )
    except AnalyticsJobUnavailable:
        return _error(404, "JOB_NOT_FOUND")
    except AnalyticsJobConflict as error:
        return _error(409, str(error))
