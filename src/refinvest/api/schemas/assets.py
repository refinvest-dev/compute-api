"""Transport models for Compute-owned asset reference data."""

from datetime import date, datetime

from pydantic import BaseModel, Field


class DataAvailabilityTransport(BaseModel):
    first_date: date = Field(alias="firstDate")
    last_date: date = Field(alias="lastDate")


class AssetTransport(BaseModel):
    symbol: str
    calendar: str
    inception_date: date = Field(alias="inceptionDate")
    execution_enabled: bool = Field(alias="executionEnabled")
    data_availability: DataAvailabilityTransport | None = Field(
        default=None, alias="dataAvailability"
    )


class AssetListTransport(BaseModel):
    assets: list[AssetTransport]
    dataset_snapshot_id: str | None = Field(default=None, alias="datasetSnapshotId")
    snapshot_created_at: datetime | None = Field(default=None, alias="snapshotCreatedAt")


class AssetAvailabilityTransport(BaseModel):
    symbol: str
    inception_date: date = Field(alias="inceptionDate")
    data_availability: DataAvailabilityTransport = Field(alias="dataAvailability")
    dataset_snapshot_id: str = Field(alias="datasetSnapshotId")
    snapshot_created_at: datetime = Field(alias="snapshotCreatedAt")
