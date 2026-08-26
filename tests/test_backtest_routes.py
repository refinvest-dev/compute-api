from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from refinvest.api.dependencies.backtests import get_backtest_job_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import app
from refinvest.application.backtests import (
    BacktestJob,
    BacktestJobRepository,
    BacktestJobStatusView,
)


class FakeBacktestJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, BacktestJob] = {}

    def create(self, job: BacktestJob) -> None:
        self.jobs[job.run_id] = job

    def get(self, run_id: str) -> BacktestJobStatusView | None:
        job = self.jobs.get(run_id)
        if job is None:
            return None
        return BacktestJobStatusView(
            run_id=job.run_id,
            strategy_version_id=job.strategy_version_id,
            dataset_snapshot_id=job.dataset_snapshot_id,
            status=job.status,
        )


def valid_request() -> dict[str, object]:
    return {
        "strategyVersionId": "strategy-v1",
        "strategyVersion": {
            "primarySignalAsset": "QQQ",
            "conditions": [
                {
                    "operator": "GT",
                    "operandA": {"asset": "QQQ", "metric": "RETURN", "window": 5},
                    "operandB": 0.01,
                }
            ],
            "executionAsset": "TQQQ",
            "lag": 0,
            "exit": {"holdingSignalSessions": 5},
        },
        "feeModel": {"commission": 0.001, "slippage": 0.002},
        "period": {"start": "2024-01-01", "end": "2024-12-31"},
        "datasetSnapshotId": "snapshot-1",
    }


@contextmanager
def configured_client(repository: BacktestJobRepository) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: Settings("test-key")
    app.dependency_overrides[get_backtest_job_repository] = lambda: repository
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_backtest_submission_is_authenticated_and_durable() -> None:
    repository = FakeBacktestJobRepository()
    with configured_client(repository) as client:
        assert client.post("/backtests", json=valid_request()).status_code == 401

        response = client.post(
            "/backtests",
            headers={"X-Internal-Api-Key": "test-key"},
            json=valid_request(),
        )
        assert response.status_code == 202
        assert response.json()["status"] == "PENDING"
        run_id = response.json()["runId"]
        assert repository.jobs[run_id].request_payload["datasetSnapshotId"] == "snapshot-1"

        status_response = client.get(
            f"/backtests/{run_id}", headers={"X-Internal-Api-Key": "test-key"}
        )
        assert status_response.status_code == 200
        assert status_response.json() == {
            "runId": run_id,
            "strategyVersionId": "strategy-v1",
            "status": "PENDING",
            "datasetSnapshotId": "snapshot-1",
        }
        payload = valid_request()
        payload["strategyVersion"] = {
            **payload["strategyVersion"],
            "conditions": [
                {
                    "operator": "GT",
                    "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": 1},
                    "operandB": 0.0,
                }
            ],
        }
        response = client.post(
            "/backtests", headers={"X-Internal-Api-Key": "test-key"}, json=payload
        )
        assert response.status_code == 400
        assert response.json()["errorCode"] == "DSL_INVALID"
        assert len(repository.jobs) == 1
