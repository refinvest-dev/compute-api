"""Internal runtime cleanup transport; cancellation acknowledgement is not absence."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse

from refinvest.api.dependencies.trading_identity import TradingIdentityRoute, trading_http_error
from refinvest.api.dependencies.trading_normalization_deletion import deletion_repository
from refinvest.api.schemas.trading_normalization_deletion import (
    RuntimeDeletionRequest,
    RuntimeDeletionResponse,
)
from refinvest.api.security import require_internal_api_key
from refinvest.application.normalization_deletion import (
    DeleteNormalizationRuntime,
    NormalizationRuntimeDeletion,
)
from refinvest.application.trading_review_jobs import TradingReviewJobStateConflict

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
