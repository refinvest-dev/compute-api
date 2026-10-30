"""HTTP characterization of pinned snapshot reads and immutable artifact failures."""

import pytest
from starlette.testclient import TestClient

import refinvest.api.routes.assets as asset_routes
import refinvest.api.routes.series as series_routes
from refinvest.api.dependencies.assets import get_dataset_snapshot_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import create_app
from tests.snapshot_query_support import QuerySnapshots, query_fixture

HEADERS = {"X-Internal-Api-Key": "synthetic-query-key"}
SERIES = "/series?symbols=QQQ,BTCUSDT&metric=PRICE&start=2025-01-03&end=2025-01-05"


@pytest.fixture(params=["filesystem", "s3"])
def query_api(request, tmp_path, monkeypatch):
    snapshot, loader, storage = query_fixture(tmp_path, request.param)
    snapshots = QuerySnapshots(snapshot)
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings("synthetic-query-key")
    application.dependency_overrides[get_dataset_snapshot_repository] = lambda: snapshots
    monkeypatch.setattr(asset_routes, "create_snapshot_execution_data_loader", lambda _: loader)
    monkeypatch.setattr(series_routes, "create_snapshot_execution_data_loader", lambda _: loader)
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
    assert loader.snapshots == [snapshot.snapshot_id] * 5
    if backend == "s3":
        assert len(storage.reads) == 15


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
    assert len(loader.snapshots) >= 2


def test_unknown_asset_invalid_series_and_unauthenticated_request_do_not_load(query_api):
    client, _, snapshots, loader, _, _, _ = query_api
    assert client.get("/assets/VIX/availability", headers=HEADERS).status_code == 404
    assert client.get(SERIES.replace("QQQ,BTCUSDT", "QQQ,QQQ"), headers=HEADERS).status_code == 400
    assert client.get(SERIES, headers={}).status_code == 401
    assert snapshots.latest_calls == 0 and loader.snapshots == []
