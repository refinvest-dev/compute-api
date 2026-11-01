"""Internal runtime deletion request and receipt transport models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
