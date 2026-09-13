"""S3 adapters for immutable, manifest-verified DatasetSnapshot artifacts."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import urlparse

from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionData
from refinvest.ingestion.snapshot.artifacts import (
    PublishedSnapshotArtifacts,
    SnapshotPublicationError,
    build_snapshot_artifact_contents,
    published_snapshot_artifacts,
)
from refinvest.ingestion.snapshot.calendar import CalendarComponent
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    DailyBar,
    ReferenceClose,
)
from refinvest.persistence.object_storage.snapshot_artifacts import (
    SnapshotArtifactError,
    load_snapshot_execution_data,
)


class S3ResponseBody(Protocol):
    def read(self) -> bytes: ...


class S3ObjectClient(Protocol):
    """Small S3 boundary; production uses boto3 and tests use an in-memory fake."""

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str) -> object: ...


class S3SnapshotArtifactPublisher:
    """Publish objects once and make the manifest the final visible entry point."""

    def __init__(self, client: S3ObjectClient) -> None:
        self._client = client

    def publish(
        self,
        snapshot: DatasetSnapshot,
        provenance: CalendarProvenance,
        sessions: Mapping[AssetSession, CalendarSession],
        daily_bars: Iterable[DailyBar],
        reference_closes: Iterable[ReferenceClose] = (),
        *,
        calendar_components: Sequence[CalendarComponent] = (),
        corporate_actions_applied: bool,
        point_in_time_validation_passed: bool,
    ) -> PublishedSnapshotArtifacts:
        location = _s3_location(snapshot.storage_path)
        contents = build_snapshot_artifact_contents(
            snapshot,
            provenance,
            sessions,
            daily_bars,
            reference_closes,
            calendar_components=calendar_components,
            corporate_actions_applied=corporate_actions_applied,
            point_in_time_validation_passed=point_in_time_validation_passed,
        )
        try:
            for name, content in contents.objects_before_manifest():
                self._put_once(location, name, content)
            self._put_once(location, "manifest.json", contents.manifest)
        except Exception as error:
            raise SnapshotPublicationError("S3 snapshot artifact publication failed.") from error
        return published_snapshot_artifacts(snapshot, contents)

    def _put_once(self, location: "S3Location", name: str, content: bytes) -> None:
        self._client.put_object(
            Bucket=location.bucket,
            Key=location.object_key(name),
            Body=content,
            IfNoneMatch="*",
        )


class S3SnapshotExecutionDataLoader:
    """Load only manifest-declared objects from one pinned S3 snapshot prefix."""

    def __init__(self, client: S3ObjectClient) -> None:
        self._client = client

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        location = _s3_location(snapshot.storage_path)
        return load_snapshot_execution_data(snapshot, lambda name: self._read(location, name))

    def _read(self, location: "S3Location", name: Path) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=location.bucket, Key=location.object_key(name)
            )
            body = response.get("Body")
            if not hasattr(body, "read"):
                raise SnapshotArtifactError("S3 object response has no readable body.")
            content = cast(S3ResponseBody, body).read()
            return content
        except SnapshotArtifactError:
            raise
        except Exception as error:
            raise SnapshotArtifactError(f"Snapshot artifact is unavailable: {name.name}") from error


class SnapshotStorageExecutionDataLoader:
    """Dispatch pinned artifact reads by immutable storage-path scheme."""

    def __init__(self, s3_client_factory: Callable[[], S3ObjectClient]) -> None:
        self._s3_client_factory = s3_client_factory
        self._s3: S3SnapshotExecutionDataLoader | None = None

    def load(self, snapshot: DatasetSnapshot) -> SnapshotExecutionData:
        if snapshot.storage_path.startswith("s3://"):
            if self._s3 is None:
                self._s3 = S3SnapshotExecutionDataLoader(self._s3_client_factory())
            return self._s3.load(snapshot)
        from refinvest.persistence.object_storage.snapshot_artifacts import (
            FilesystemSnapshotExecutionDataLoader,
        )

        return FilesystemSnapshotExecutionDataLoader().load(snapshot)


class S3Location:
    def __init__(self, bucket: str, prefix: PurePosixPath) -> None:
        self.bucket = bucket
        self._prefix = prefix

    def object_key(self, name: str | Path) -> str:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("S3 snapshot object names must be normalized relative paths.")
        return (self._prefix / path).as_posix()


def _s3_location(storage_path: str) -> S3Location:
    parsed = urlparse(storage_path)
    prefix = PurePosixPath(parsed.path.lstrip("/"))
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or not parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or prefix.is_absolute()
        or "." in prefix.parts
        or ".." in prefix.parts
        or parsed.path.lstrip("/") != prefix.as_posix()
    ):
        raise ValueError("Snapshot S3 storage_path must be s3://bucket/normalized-prefix.")
    return S3Location(parsed.netloc, prefix)
