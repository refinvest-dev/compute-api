"""Internal runtime cleanup transport; cancellation acknowledgement is not absence."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from refinvest.api.routes.trading_review_jobs import TradingIdentityRoute, trading_http_error
from refinvest.api.security import require_internal_api_key
from refinvest.app.config import Settings, get_settings
from refinvest.application.normalization_deletion import (
    DeleteNormalizationRuntime,
    NormalizationRuntimeDeletion,
)
from refinvest.application.trading_review_jobs import TradingReviewJobStateConflict
from refinvest.persistence.postgres.normalization_deletion import (
    PostgresNormalizationRuntimeDeletion,
)


def deletion_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> NormalizationRuntimeDeletion:
    if settings.database_url is None:
        raise HTTPException(status_code=503, detail="Deletion store is unavailable")
    return PostgresNormalizationRuntimeDeletion(settings.database_url)


class RuntimeDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    coreImportSessionId: str = Field(min_length=1, max_length=128)
    sourceDeletionGeneration: int = Field(ge=0)
    deletionRequestId: str = Field(min_length=1, max_length=128)
    deletionGeneration: int = Field(ge=1)


class RuntimeDeletionResponse(RuntimeDeletionRequest):
    coreNormalizationRunId: str
    state: Literal["PENDING", "ABSENT"]
    verifiedAbsentAt: datetime | None
    retryAfterSeconds: int | None


router = APIRouter(
    dependencies=[Depends(require_internal_api_key)], route_class=TradingIdentityRoute
)


@router.put(
    "/trading-review/normalization-runs/{coreNormalizationRunId}/deletion",
    operation_id="deleteTradingNormalizationRuntime",
    response_model=None,
)
def delete_normalization_runtime(
    coreNormalizationRunId: Annotated[str, Path(min_length=1, max_length=128)],
    body: RuntimeDeletionRequest,
    repository: Annotated[NormalizationRuntimeDeletion, Depends(deletion_repository)],
) -> RuntimeDeletionResponse | JSONResponse:
    command = DeleteNormalizationRuntime(
        coreNormalizationRunId,
        body.coreImportSessionId,
        body.sourceDeletionGeneration,
        body.deletionRequestId,
        body.deletionGeneration,
    )
    try:
        result = repository.purge(command)
        if result.identity != command:
            raise RuntimeError("Deletion response identity mismatch")
    except TradingReviewJobStateConflict:
        return trading_http_error(409, "DELETION_IDENTITY_CONFLICT", "Deletion identity conflicts")
    except Exception:
        return trading_http_error(
            503, "TEMPORARY_DEPENDENCY_FAILURE", "Runtime deletion is unavailable", True
        )
    return RuntimeDeletionResponse(
        **body.model_dump(),
        coreNormalizationRunId=command.run_id,
        state="ABSENT" if result.verified_at is not None else "PENDING",
        verifiedAbsentAt=result.verified_at,
        retryAfterSeconds=None if result.verified_at is not None else 5,
    )
