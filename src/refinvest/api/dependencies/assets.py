"""Asset-reference dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, status

from refinvest.app.config import Settings, get_settings
from refinvest.app.snapshot_storage import create_snapshot_execution_data_loader
from refinvest.application.dataset_snapshots import DatasetSnapshotRepository
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository


def get_dataset_snapshot_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatasetSnapshotRepository:
    if settings.database_url is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PostgresDatasetSnapshotRepository(settings.database_url)


def get_snapshot_execution_data_loader(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SnapshotExecutionDataLoader:
    """Compose a fresh loader per request; storage clients remain lazy until load."""
    return create_snapshot_execution_data_loader(settings)
