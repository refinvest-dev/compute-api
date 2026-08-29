"""Authenticated Backtest submission and status routes."""

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Annotated, cast

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from refinvest.api.dependencies.backtests import get_backtest_job_repository
from refinvest.api.schemas.backtests import (
    BacktestAcceptedTransport,
    BacktestStatusTransport,
    CreateBacktestRequestTransport,
    FatalErrorCodeTransport,
    PeriodTransport,
)
from refinvest.api.schemas.errors import map_strategy_validation_error
from refinvest.app.security import require_internal_api_key
from refinvest.application.backtests import BacktestJobRepository, submit_backtest
from refinvest.engine.validation import StrategyValidationError

router = APIRouter(
    prefix="/backtests",
    tags=["backtests"],
    dependencies=[Depends(require_internal_api_key)],
)

_RESULT_NUMBER_FIELDS = frozenset({
    "totalReturn", "cagr", "mdd", "sharpe", "winRate", "avgTradeReturn",
    "avgHoldingPeriod", "profitFactor", "value", "entryPrice", "exitPrice", "returnPct",
    "median", "max",
})
_RESULT_NUMBER_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


@router.post("", response_model=BacktestAcceptedTransport, status_code=status.HTTP_202_ACCEPTED)
def create_backtest(
    request: CreateBacktestRequestTransport,
    repository: Annotated[BacktestJobRepository, Depends(get_backtest_job_repository)],
) -> BacktestAcceptedTransport | JSONResponse:
    try:
        accepted = submit_backtest(request.to_domain(), repository)
    except StrategyValidationError as error:
        response = map_strategy_validation_error(error)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content=response.model_dump(by_alias=True)
        )
    return BacktestAcceptedTransport(
        runId=accepted.run_id,
        strategyVersionId=accepted.strategy_version_id,
        status="PENDING",
    )


@router.get(
    "/{run_id}", response_model=BacktestStatusTransport, response_model_exclude_none=True
)
def get_backtest_status(
    run_id: str,
    repository: Annotated[BacktestJobRepository, Depends(get_backtest_job_repository)],
) -> BacktestStatusTransport | JSONResponse:
    job = repository.get(run_id)
    if job is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"message": "Backtest job not found."}
        )
    return BacktestStatusTransport(
        runId=job.run_id,
        strategyVersionId=job.strategy_version_id,
        datasetSnapshotId=job.dataset_snapshot_id,
        status=job.status.value,
        actualPeriod=(
            PeriodTransport(start=job.actual_period.start, end=job.actual_period.end)
            if job.actual_period is not None
            else None
        ),
        engineVersion=job.engine_version,
        result=_response_result_payload(job.result_payload),
        failureReason=job.failure_reason,
        errorCode=FatalErrorCodeTransport(job.error_code) if job.error_code is not None else None,
    )


def _response_result_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    return cast(dict[str, object], _response_value(payload, None))


def _response_value(value: object, key: str | None) -> object:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        return {
            item_key: _response_value(item_value, item_key)
            for item_key, item_value in mapping.items()
        }
    if isinstance(value, list):
        items = cast(list[object], value)
        return [_response_value(item, key) for item in items]
    if key in _RESULT_NUMBER_FIELDS and isinstance(value, str):
        with localcontext(_RESULT_NUMBER_CONTEXT):
            return float(Decimal(value).quantize(Decimal("0.000000000001")))
    return value
