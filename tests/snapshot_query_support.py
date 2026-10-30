"""Non-production immutable snapshots for query boundary and cost checks."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO

from refinvest.application.assets import ASSET_REFERENCES
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import build_snapshot_artifact_contents
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)
from refinvest.persistence.object_storage.s3_snapshot_artifacts import (
    SnapshotStorageExecutionDataLoader,
)


class QuerySnapshots:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.latest_calls = 0
        self.get_calls = []

    def latest(self):
        self.latest_calls += 1
        return self.snapshot

    def get(self, snapshot_id):
        self.get_calls.append(snapshot_id)
        return self.snapshot if self.snapshot and self.snapshot.snapshot_id == snapshot_id else None


class QueryS3:
    def __init__(self, objects):
        self.objects = objects
        self.reads = []

    def get_object(self, *, Bucket, Key):
        assert Bucket == "synthetic-query-bucket"
        self.reads.append(Key)
        return {"Body": BytesIO(self.objects[Key.rsplit("/", 1)[1]])}


class CountingQueryLoader:
    def __init__(self, loader):
        self.loader = loader
        self.snapshots = []

    def load(self, snapshot):
        self.snapshots.append(snapshot.snapshot_id)
        return self.loader.load(snapshot)


def query_fixture(
    tmp_path,
    backend,
    *,
    start=date(2025, 1, 2),
    end=date(2025, 1, 8),
    snapshot_id="query-snapshot-1",
):
    provenance = CalendarProvenance("synthetic-query", "v1", "five-assets-query-v1")
    snapshot = DatasetSnapshot(
        snapshot_id,
        datetime(2025, 1, 9, tzinfo=UTC),
        "synthetic-query",
        {"firstDate": start.isoformat(), "lastDate": end.isoformat()},
        "adjusted",
        str(tmp_path),
        provenance.source,
        provenance.version,
        provenance.content_hash,
    )
    sessions = {}
    bars = []
    for index in range((end - start).days + 1):
        day = start + timedelta(days=index)
        for asset in ASSET_REFERENCES:
            if asset.symbol != "BTCUSDT" and day.weekday() > 4:
                continue
            completion = (
                datetime.combine(day + timedelta(days=1), time.min, UTC)
                if asset.symbol == "BTCUSDT"
                else datetime.combine(day, time(21), UTC)
            )
            sessions[AssetSession(asset.symbol, day)] = CalendarSession(
                CalendarSessionState.EXPECTED, completion
            )
            price = Decimal(100) + Decimal(index)
            bars.append(DailyBar(asset.symbol, day, price, price, price, price, Decimal(1000)))
    contents = build_snapshot_artifact_contents(
        snapshot,
        provenance,
        sessions,
        bars,
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )
    objects = dict(contents.objects_before_manifest())
    objects["manifest.json"] = contents.manifest
    client = QueryS3(objects)
    if backend == "s3":
        snapshot = replace(snapshot, storage_path=f"s3://synthetic-query-bucket/{snapshot_id}")
    else:
        tmp_path.mkdir(parents=True, exist_ok=True)
        for name, content in objects.items():
            (tmp_path / name).write_bytes(content)
    return snapshot, CountingQueryLoader(SnapshotStorageExecutionDataLoader(lambda: client)), client
