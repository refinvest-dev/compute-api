"""Asset-reference dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, status

from refinvest.app.config import Settings, get_settings
from refinvest.persistence.postgres.dataset_snapshots import PostgresDatasetSnapshotRepository


def get_dataset_snapshot_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PostgresDatasetSnapshotRepository:
    if settings.database_url is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PostgresDatasetSnapshotRepository(settings.database_url)
