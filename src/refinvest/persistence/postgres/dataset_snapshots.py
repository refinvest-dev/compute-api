"""PostgreSQL adapter for immutable DatasetSnapshot metadata."""

from datetime import UTC, datetime
from typing import cast

from psycopg import connect
from psycopg.types.json import Jsonb

from refinvest.application.dataset_snapshots import DatasetSnapshot, SourceArtifact


class PostgresDatasetSnapshotRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def get(self, snapshot_id: str) -> DatasetSnapshot | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot_id, created_at, source, coverage, adjustment_policy, storage_path,
                    calendar_source, calendar_version, calendar_hash, source_artifacts
                FROM compute_dataset_snapshots
                WHERE snapshot_id = %s
                """,
                (snapshot_id,),
            )
            return _snapshot_from_row(cursor.fetchone())

    def latest(self) -> DatasetSnapshot | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot_id, created_at, source, coverage, adjustment_policy, storage_path,
                    calendar_source, calendar_version, calendar_hash, source_artifacts
                FROM compute_dataset_snapshots
                ORDER BY created_at DESC, snapshot_id DESC
                LIMIT 1
                """,
            )
            return _snapshot_from_row(cursor.fetchone())

    def create(self, snapshot: DatasetSnapshot) -> None:
        """Insert immutable metadata only after its artifact has been published."""
        _insert_snapshot(self._database_url, snapshot)


def _snapshot_from_row(row: tuple[object, ...] | None) -> DatasetSnapshot | None:
    if row is None:
        return None
    (
        snapshot_id,
        created_at,
        source,
        coverage,
        adjustment_policy,
        storage_path,
        calendar_source,
        calendar_version,
        calendar_hash,
        source_artifacts,
    ) = cast(
        tuple[
            str,
            datetime,
            str,
            dict[str, object],
            str,
            str,
            str | None,
            str | None,
            str | None,
            list[dict[str, object]],
        ],
        row,
    )
    return DatasetSnapshot(
        snapshot_id,
        created_at,
        source,
        coverage,
        adjustment_policy,
        storage_path,
        calendar_source,
        calendar_version,
        calendar_hash,
        _source_artifacts(source_artifacts),
    )


def insert_dataset_snapshot(database_url: str, snapshot: DatasetSnapshot) -> None:
    """Used by future ingestion code; existing snapshots are never updated."""
    _insert_snapshot(database_url, snapshot)


def _insert_snapshot(database_url: str, snapshot: DatasetSnapshot) -> None:
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO compute_dataset_snapshots
                    (snapshot_id, created_at, source, coverage, adjustment_policy, storage_path,
                    calendar_source, calendar_version, calendar_hash, source_artifacts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                snapshot.snapshot_id,
                snapshot.created_at,
                snapshot.source,
                Jsonb(snapshot.coverage),
                snapshot.adjustment_policy,
                snapshot.storage_path,
                snapshot.calendar_source,
                snapshot.calendar_version,
                snapshot.calendar_hash,
                Jsonb(
                    [
                        {
                            "uri": artifact.uri,
                            "sha256": artifact.sha256,
                            "retrievedAt": artifact.retrieved_at.isoformat(),
                        }
                        for artifact in snapshot.source_artifacts
                    ]
                ),
            ),
        )


def _source_artifacts(value: list[dict[str, object]]) -> tuple[SourceArtifact, ...]:
    try:
        return tuple(
            SourceArtifact(
                str(item["uri"]),
                str(item["sha256"]),
                datetime.fromisoformat(str(item["retrievedAt"])).astimezone(UTC),
            )
            for item in value
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("DatasetSnapshot source_artifacts are invalid.") from error
