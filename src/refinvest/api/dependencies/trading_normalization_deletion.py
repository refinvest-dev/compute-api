"""Runtime deletion repository composition, independent of payload encryption keys."""

from typing import Annotated

from fastapi import Depends, HTTPException

from refinvest.app.config import Settings, get_settings
from refinvest.application.normalization_deletion import NormalizationRuntimeDeletion
from refinvest.persistence.postgres.normalization_deletion import (
    PostgresNormalizationRuntimeDeletion,
)


def deletion_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> NormalizationRuntimeDeletion:
    if settings.database_url is None:
        raise HTTPException(status_code=503, detail="Deletion store is unavailable")
    return PostgresNormalizationRuntimeDeletion(settings.database_url)
