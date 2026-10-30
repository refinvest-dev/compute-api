"""Core-only immutable snapshot Data Explorer series route."""

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from refinvest.api.dependencies.assets import (
    get_dataset_snapshot_repository,
    get_snapshot_execution_data_loader,
)
from refinvest.api.security import require_internal_api_key
from refinvest.application.dataset_snapshots import DatasetSnapshotRepository
from refinvest.application.series import build_series
from refinvest.application.snapshot_execution_data import (
    SnapshotArtifactError,
    SnapshotExecutionDataLoader,
)
from refinvest.marketdata.ports import MissingRequiredMarketDataError

router = APIRouter(tags=["assets"], dependencies=[Depends(require_internal_api_key)])


@router.get("/series", response_model=None)
def get_series(
    symbols: str,
    metric: Literal["PRICE", "RETURN", "NORMALIZED"],
    start: date,
    end: date,
    snapshots: Annotated[DatasetSnapshotRepository, Depends(get_dataset_snapshot_repository)],
    loader: Annotated[SnapshotExecutionDataLoader, Depends(get_snapshot_execution_data_loader)],
    dataset_snapshot_id: str | None = Query(default=None, alias="datasetSnapshotId"),
) -> dict[str, object] | JSONResponse:
    requested = tuple(symbols.split(","))
    if (
        not symbols
        or len(requested) > 5
        or len(set(requested)) != len(requested)
        or start > end
        or _exceeds_maximum_period(start, end)
    ):
        return JSONResponse(status_code=400, content={"message": "Invalid series request."})
    snapshot = snapshots.get(dataset_snapshot_id) if dataset_snapshot_id else snapshots.latest()
    if snapshot is None:
        return JSONResponse(
            status_code=404 if dataset_snapshot_id else 503,
            content=(
                {"message": "DatasetSnapshot was not found."}
                if dataset_snapshot_id
                else {
                    "message": "DatasetSnapshot is unavailable.",
                    "errorCode": "DATASET_UNAVAILABLE",
                }
            ),
        )
    try:
        series = build_series(
            snapshot,
            loader,
            requested,
            metric,
            start,
            end,
        )
    except (MissingRequiredMarketDataError, SnapshotArtifactError) as error:
        return JSONResponse(
            status_code=503,
            content={"message": str(error), "errorCode": "DATASET_CORRUPTION"},
        )
    except ValueError as error:
        return JSONResponse(status_code=400, content={"message": str(error)})
    points = sum(len(values) for values in series.values())
    if points > 20_000:
        return JSONResponse(status_code=400, content={"message": "SERIES_LIMIT_EXCEEDED"})
    return {
        "datasetSnapshotId": snapshot.snapshot_id,
        "snapshotCreatedAt": snapshot.created_at,
        "adjustmentPolicy": snapshot.adjustment_policy,
        "series": [
            {
                "symbol": symbol,
                "points": [
                    {"date": point.session_date, "value": _json_number(point.value)}
                    for point in values
                ],
            }
            for symbol, values in series.items()
        ],
    }


def _exceeds_maximum_period(start: date, end: date) -> bool:
    try:
        maximum_end = start.replace(year=start.year + 10)
    except ValueError:
        maximum_end = start.replace(year=start.year + 10, day=28)
    return end > maximum_end


def _json_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN))
