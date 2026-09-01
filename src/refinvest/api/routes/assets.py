"""Core-only immutable asset catalog and latest-snapshot availability routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from refinvest.api.dependencies.assets import get_dataset_snapshot_repository
from refinvest.api.schemas.assets import (
    AssetAvailabilityTransport,
    AssetListTransport,
    AssetTransport,
    DataAvailabilityTransport,
)
from refinvest.app.config import Settings, get_settings
from refinvest.app.security import require_internal_api_key
from refinvest.app.snapshot_storage import create_snapshot_execution_data_loader
from refinvest.application.assets import (
    ASSET_REFERENCES,
    AssetReference,
    asset_reference,
    availability_for_snapshot,
)
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.persistence.object_storage.s3_snapshot_artifacts import (
    SnapshotStorageExecutionDataLoader,
)
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository

router = APIRouter(
    prefix="/assets", tags=["assets"], dependencies=[Depends(require_internal_api_key)]
)


@router.get("", response_model=AssetListTransport, response_model_exclude_none=True)
def list_assets(
    snapshots: Annotated[
        PostgresDatasetSnapshotRepository, Depends(get_dataset_snapshot_repository)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssetListTransport:
    snapshot = snapshots.latest()
    loader = create_snapshot_execution_data_loader(settings) if snapshot else None
    values = [_asset_transport(asset, snapshot, loader) for asset in ASSET_REFERENCES]
    return AssetListTransport(
        assets=values,
        datasetSnapshotId=snapshot.snapshot_id if snapshot else None,
        snapshotCreatedAt=snapshot.created_at if snapshot else None,
    )


@router.get("/{symbol}/availability", response_model=AssetAvailabilityTransport)
def get_asset_availability(
    symbol: str,
    snapshots: Annotated[
        PostgresDatasetSnapshotRepository, Depends(get_dataset_snapshot_repository)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssetAvailabilityTransport | JSONResponse:
    asset = asset_reference(symbol)
    if asset is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"message": "Asset not found."}
        )
    snapshot = snapshots.latest()
    if snapshot is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "message": "DatasetSnapshot is unavailable.",
                "errorCode": "DATASET_UNAVAILABLE",
            },
        )
    availability = availability_for_snapshot(
        asset, snapshot, create_snapshot_execution_data_loader(settings)
    )
    if availability is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "message": "Asset data is unavailable in the latest DatasetSnapshot.",
                "errorCode": "DATASET_UNAVAILABLE",
            },
        )
    return AssetAvailabilityTransport(
        symbol=asset.symbol,
        inceptionDate=asset.inception_date,
        dataAvailability=DataAvailabilityTransport(
            firstDate=availability.first_date, lastDate=availability.last_date
        ),
        datasetSnapshotId=availability.dataset_snapshot_id,
        snapshotCreatedAt=availability.snapshot_created_at,
    )


def _asset_transport(
    asset: AssetReference,
    snapshot: DatasetSnapshot | None,
    loader: SnapshotStorageExecutionDataLoader | None,
) -> AssetTransport:
    availability = (
        availability_for_snapshot(asset, snapshot, loader) if snapshot and loader else None
    )
    return AssetTransport(
        symbol=asset.symbol,
        calendar=asset.calendar,
        inceptionDate=asset.inception_date,
        executionEnabled=asset.execution_enabled,
        dataAvailability=(
            DataAvailabilityTransport(
                firstDate=availability.first_date, lastDate=availability.last_date
            )
            if availability
            else None
        ),
    )
