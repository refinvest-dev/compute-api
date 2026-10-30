"""HTTP characterization of pinned snapshot reads and immutable artifact failures."""

import pytest
from starlette.testclient import TestClient

from refinvest.api.dependencies.assets import (
    get_dataset_snapshot_repository,
    get_snapshot_execution_data_loader,
)
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import create_app
from tests.snapshot_query_support import QuerySnapshots, query_fixture

HEADERS = {"X-Internal-Api-Key": "synthetic-query-key"}
SERIES = "/series?symbols=QQQ,BTCUSDT&metric=PRICE&start=2025-01-03&end=2025-01-05"


@pytest.fixture(params=["filesystem", "s3"])
def query_api(request, tmp_path):
    snapshot, loader, storage = query_fixture(tmp_path, request.param)
    snapshots = QuerySnapshots(snapshot)
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings("synthetic-query-key")
    application.dependency_overrides[get_dataset_snapshot_repository] = lambda: snapshots
    application.dependency_overrides[get_snapshot_execution_data_loader] = lambda: loader
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client, snapshot, snapshots, loader, storage, tmp_path, request.param


def test_catalog_uses_one_snapshot_and_preserves_all_five_availabilities(query_api):
    client, snapshot, snapshots, loader, storage, _, backend = query_api
    response = client.get("/assets", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["datasetSnapshotId"] == snapshot.snapshot_id
    assert [asset["symbol"] for asset in result["assets"]] == [
        "QQQ",
        "SPY",
        "TQQQ",
        "SOXL",
        "BTCUSDT",
    ]
    assert all(
        asset["dataAvailability"] == {"firstDate": "2025-01-02", "lastDate": "2025-01-08"}
        for asset in result["assets"]
    )
    assert snapshots.latest_calls == 1
    assert loader.snapshots == [snapshot.snapshot_id]
    if backend == "s3":
        assert len(storage.reads) == 3


def test_explicit_series_pin_uses_get_once_and_preserves_independent_sessions(query_api):
    client, snapshot, snapshots, loader, _, _, _ = query_api
    response = client.get(SERIES + "&datasetSnapshotId=" + snapshot.snapshot_id, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["datasetSnapshotId"] == snapshot.snapshot_id
    assert snapshots.latest_calls == 0 and snapshots.get_calls == [snapshot.snapshot_id]
    assert loader.snapshots == [snapshot.snapshot_id]
    assert result["series"] == [
        {"symbol": "QQQ", "points": [{"date": "2025-01-03", "value": 101.0}]},
        {
            "symbol": "BTCUSDT",
            "points": [
                {"date": "2025-01-03", "value": 101.0},
                {"date": "2025-01-04", "value": 102.0},
                {"date": "2025-01-05", "value": 103.0},
            ],
        },
    ]


def test_requests_without_snapshot_or_with_unknown_pin_do_not_read_artifacts(query_api):
    client, _, snapshots, loader, _, _, _ = query_api
    snapshots.snapshot = None
    catalog = client.get("/assets", headers=HEADERS)
    assert catalog.status_code == 200 and "datasetSnapshotId" not in catalog.json()
    assert all("dataAvailability" not in row for row in catalog.json()["assets"])
    assert client.get("/assets/QQQ/availability", headers=HEADERS).json() == {
        "message": "DatasetSnapshot is unavailable.",
        "errorCode": "DATASET_UNAVAILABLE",
    }
    assert client.get(SERIES, headers=HEADERS).status_code == 503
    assert client.get(SERIES + "&datasetSnapshotId=unknown", headers=HEADERS).status_code == 404
    assert loader.snapshots == []


@pytest.mark.parametrize("path", ["/assets", "/assets/QQQ/availability", SERIES])
def test_each_request_revalidates_artifacts_and_retains_error_responses(query_api, path):
    client, _, _, loader, storage, root, backend = query_api
    assert client.get(path, headers=HEADERS).status_code == 200
    if backend == "s3":
        storage.objects["daily-bars.ndjson"] = b"tampered"
    else:
        (root / "daily-bars.ndjson").write_bytes(b"tampered")
    response = client.get(path, headers=HEADERS)
    if path == SERIES:
        assert response.status_code == 503
        assert response.json() == {
            "message": "Unexpected artifact SHA-256 for daily-bars.ndjson.",
            "errorCode": "DATASET_CORRUPTION",
        }
    else:
        assert response.status_code == 500
    assert len(loader.snapshots) == 2


def test_unknown_asset_invalid_series_and_unauthenticated_request_do_not_load(query_api):
    client, _, snapshots, loader, _, _, _ = query_api
    assert client.get("/assets/VIX/availability", headers=HEADERS).status_code == 404
    assert client.get(SERIES.replace("QQQ,BTCUSDT", "QQQ,QQQ"), headers=HEADERS).status_code == 400
    assert client.get(SERIES, headers={}).status_code == 401
    assert snapshots.latest_calls == 0 and loader.snapshots == []


def test_new_request_resolves_latest_again_without_reusing_an_earlier_snapshot(query_api):
    client, snapshot, snapshots, loader, _, root, backend = query_api
    assert (
        client.get("/assets", headers=HEADERS).json()["datasetSnapshotId"] == snapshot.snapshot_id
    )
    newer, new_loader, _ = query_fixture(root / "next", backend, snapshot_id="query-snapshot-2")
    snapshots.snapshot = newer
    client.app.dependency_overrides[get_snapshot_execution_data_loader] = lambda: new_loader
    result = client.get("/assets", headers=HEADERS).json()
    assert result["datasetSnapshotId"] == newer.snapshot_id
    assert snapshots.latest_calls == 2
    assert loader.snapshots == [snapshot.snapshot_id]
    assert new_loader.snapshots == [newer.snapshot_id]


def test_real_provider_creates_request_scoped_loaders_without_opening_s3_for_empty_catalog(
    monkeypatch,
):
    import refinvest.api.dependencies.assets as dependencies

    settings = Settings("synthetic-query-key")
    created = []

    def factory(received):
        assert received is settings

        class Loader:
            def load(self, snapshot):
                raise AssertionError(
                    "No snapshot must not read artifacts or construct an S3 client."
                )

        loader = Loader()
        created.append(loader)
        return loader

    monkeypatch.setattr(dependencies, "create_snapshot_execution_data_loader", factory)
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_dataset_snapshot_repository] = lambda: QuerySnapshots(None)
    with TestClient(application) as client:
        assert client.get("/assets", headers=HEADERS).status_code == 200
        assert client.get("/assets", headers=HEADERS).status_code == 200
    assert len(created) == 2 and created[0] is not created[1]
