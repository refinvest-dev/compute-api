"""Deployment composition for immutable snapshot storage backends."""

from collections.abc import Callable
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from refinvest.app.config import Settings
from refinvest.ingestion.snapshot.artifacts import FilesystemSnapshotArtifactPublisher
from refinvest.ingestion.snapshot.workflow import SnapshotArtifactPublisher
from refinvest.persistence.object_storage.s3_snapshot_artifacts import (
    S3ObjectClient,
    S3SnapshotArtifactPublisher,
    SnapshotStorageExecutionDataLoader,
)


def create_snapshot_artifact_publisher(settings: Settings) -> SnapshotArtifactPublisher:
    """Select the configured immutable writer without changing the artifact contract."""
    if settings.snapshot_storage_backend == "filesystem":
        return FilesystemSnapshotArtifactPublisher()
    if settings.snapshot_storage_backend == "s3":
        return S3SnapshotArtifactPublisher(create_s3_object_client(settings))
    raise ValueError("REFINVEST_SNAPSHOT_STORAGE_BACKEND must be filesystem or s3.")


def create_snapshot_execution_data_loader(settings: Settings) -> SnapshotStorageExecutionDataLoader:
    """Read filesystem history and S3 history from their pinned storage paths."""
    return SnapshotStorageExecutionDataLoader(lambda: create_s3_object_client(settings))


def snapshot_storage_path(settings: Settings, snapshot_id: str) -> str:
    """Return one immutable location; no backend may reuse an existing snapshot ID."""
    if settings.snapshot_storage_backend == "filesystem":
        if not settings.snapshot_storage_root:
            raise ValueError("REFINVEST_SNAPSHOT_STORAGE_ROOT is required for filesystem storage.")
        return str(Path(settings.snapshot_storage_root) / snapshot_id)
    if settings.snapshot_storage_backend == "s3":
        bucket = _required_s3_value(settings.snapshot_storage_s3_bucket, "S3_BUCKET")
        prefix = _normalized_s3_prefix(settings.snapshot_storage_s3_prefix)
        return f"s3://{bucket}/{prefix}/{snapshot_id}"
    raise ValueError("REFINVEST_SNAPSHOT_STORAGE_BACKEND must be filesystem or s3.")


def create_s3_object_client(settings: Settings) -> S3ObjectClient:
    """Use the AWS SDK default credential chain, normally the EC2 instance role."""

    boto3_session = import_module("boto3.session")
    session_factory = cast(Callable[..., S3Session], boto3_session.Session)
    session = session_factory(region_name=settings.snapshot_storage_s3_region)
    return cast(S3ObjectClient, session.client("s3"))


class S3Session(Protocol):
    def client(self, service_name: str) -> object: ...


def _required_s3_value(value: str | None, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"REFINVEST_SNAPSHOT_STORAGE_{name} is required for S3 storage.")
    return value


def _normalized_s3_prefix(value: str | None) -> str:
    prefix = _required_s3_value(value, "S3_PREFIX")
    parsed = PurePosixPath(prefix)
    if parsed.is_absolute() or "." in parsed.parts or ".." in parsed.parts:
        raise ValueError("REFINVEST_SNAPSHOT_STORAGE_S3_PREFIX must be a normalized prefix.")
    return parsed.as_posix()
