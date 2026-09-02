from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from uuid import UUID

from starlette.testclient import TestClient

from refinvest.api.dependencies.backtests import get_backtest_job_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import app
from refinvest.application.backtests import (
    BacktestCapacityExceeded,
    BacktestIdempotencyConflict,
    BacktestJob,
    BacktestJobRepository,
    BacktestJobStatus,
    BacktestJobStatusView,
    BacktestSubmission,
)
from refinvest.engine.validation.models import Period


class FakeBacktestJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, BacktestJob] = {}
        self.status_views: dict[str, BacktestJobStatusView] = {}
        self.idempotency: dict[str, tuple[str, BacktestSubmission]] = {}

    def create(self, job: BacktestJob) -> None:
        self.jobs[job.run_id] = job

    def create_idempotently(
        self,
        job: BacktestJob,
        idempotency_key_hash: str,
        request_fingerprint: str,
        active_job_limit: int,
    ) -> BacktestSubmission:
        existing = self.idempotency.get(idempotency_key_hash)
        if existing is not None:
            fingerprint, submission = existing
            if fingerprint != request_fingerprint:
                raise BacktestIdempotencyConflict("different request")
            return submission
        active_jobs = sum(
            job.status in (BacktestJobStatus.PENDING, BacktestJobStatus.RUNNING)
            for job in self.jobs.values()
        )
        if active_jobs >= active_job_limit:
            raise BacktestCapacityExceeded("capacity exceeded")
        submission = BacktestSubmission(job.run_id, job.strategy_version_id)
        self.idempotency[idempotency_key_hash] = (request_fingerprint, submission)
        self.create(job)
        return submission

    def get(self, run_id: str) -> BacktestJobStatusView | None:
        if run_id in self.status_views:
            return self.status_views[run_id]
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


def headers(idempotency_key: str = "d9428888-122b-4f8a-8a95-2c5b31eb3380") -> dict[str, str]:
    return {"X-Internal-Api-Key": "test-key", "Idempotency-Key": idempotency_key}


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
            headers=headers(),
            json=valid_request(),
        )
        assert response.status_code == 202
        assert response.json()["status"] == "PENDING"
        run_id = response.json()["runId"]
        assert repository.jobs[run_id].request_payload["datasetSnapshotId"] == "snapshot-1"
        assert (
            repository.jobs[run_id].request_payload["strategyVersion"]["conditions"][0]["operandB"]
            == "0.01"
        )
        assert repository.jobs[run_id].request_payload["feeModel"] == {
            "commission": "0.001",
            "slippage": "0.002",
        }

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
            "/backtests", headers=headers(), json=payload
        )
        assert response.status_code == 400
        assert response.json()["errorCode"] == "DSL_INVALID"
        assert len(repository.jobs) == 1


def test_submission_replay_returns_the_original_acceptance_without_creating_a_job() -> None:
    repository = FakeBacktestJobRepository()
    with configured_client(repository) as client:
        first = client.post("/backtests", headers=headers(), json=valid_request())
        replay = client.post("/backtests", headers=headers(), json=valid_request())

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert len(repository.jobs) == 1


def test_submission_rejects_idempotency_key_reused_for_another_request() -> None:
    repository = FakeBacktestJobRepository()
    modified = valid_request()
    modified["datasetSnapshotId"] = "snapshot-2"
    with configured_client(repository) as client:
        assert client.post("/backtests", headers=headers(), json=valid_request()).status_code == 202
        conflict = client.post("/backtests", headers=headers(), json=modified)

    assert conflict.status_code == 409
    assert conflict.json() == {"message": "Idempotency-Key was reused for a different request."}
    assert len(repository.jobs) == 1


def test_submission_rejects_new_work_at_capacity_but_replays_existing_work() -> None:
    repository = FakeBacktestJobRepository()
    with configured_client(repository) as client:
        accepted = client.post("/backtests", headers=headers(), json=valid_request())
        for index in range(19):
            repository.jobs[f"occupied-{index}"] = BacktestJob(
                f"occupied-{index}", None, None, BacktestJobStatus.PENDING, {}
            )
        exhausted = client.post(
            "/backtests",
            headers=headers("72c6ac13-1f3b-4eec-8db3-7c7ba27df10e"),
            json=valid_request(),
        )
        replay = client.post("/backtests", headers=headers(), json=valid_request())

    assert accepted.status_code == replay.status_code == 202
    assert replay.json() == accepted.json()
    assert exhausted.status_code == 503
    assert exhausted.headers["Retry-After"] == "30"
    assert exhausted.json() == {
        "message": "Compute active-job capacity is temporarily exhausted.",
        "errorCode": "BACKTEST_CAPACITY_EXCEEDED",
    }
    assert len(repository.jobs) == 20


def test_submission_requires_uuid_v4_idempotency_key() -> None:
    repository = FakeBacktestJobRepository()
    with configured_client(repository) as client:
        missing = client.post(
            "/backtests", headers={"X-Internal-Api-Key": "test-key"}, json=valid_request()
        )
        non_v4 = client.post(
            "/backtests", headers=headers(str(UUID(int=0))), json=valid_request()
        )

    assert missing.status_code == non_v4.status_code == 400
    assert missing.json() == non_v4.json() == {"message": "Idempotency-Key must be a UUID v4."}


def test_failed_backtest_status_includes_documented_failure_fields() -> None:
    repository = FakeBacktestJobRepository()
    repository.status_views["run-failed"] = BacktestJobStatusView(
        run_id="run-failed",
        strategy_version_id="strategy-v1",
        dataset_snapshot_id="snapshot-1",
        status=BacktestJobStatus.FAILED,
        failure_reason="The pinned DatasetSnapshot is unavailable.",
        error_code="MISSING_REQUIRED_DATA",
    )
    with configured_client(repository) as client:
        response = client.get("/backtests/run-failed", headers={"X-Internal-Api-Key": "test-key"})
    assert response.status_code == 200
    assert response.json() == {
        "runId": "run-failed",
        "strategyVersionId": "strategy-v1",
        "datasetSnapshotId": "snapshot-1",
        "status": "FAILED",
        "failureReason": "The pinned DatasetSnapshot is unavailable.",
        "errorCode": "MISSING_REQUIRED_DATA",
    }


def test_completed_backtest_status_includes_result_metadata() -> None:
    repository = FakeBacktestJobRepository()
    repository.status_views["run-completed"] = BacktestJobStatusView(
        "run-completed", "strategy-v1", "snapshot-1", BacktestJobStatus.COMPLETED,
        actual_period=Period(date(2025, 1, 2), date(2025, 1, 3)), engine_version="v1",
        result_payload={"metrics": {"totalReturn": "0.12345678901234"}},
    )
    with configured_client(repository) as client:
        response = client.get(
            "/backtests/run-completed", headers={"X-Internal-Api-Key": "test-key"}
        )
    assert response.status_code == 200
    assert response.json()["actualPeriod"] == {"start": "2025-01-02", "end": "2025-01-03"}
    assert response.json()["engineVersion"] == "v1"
    assert response.json()["result"]["metrics"]["totalReturn"] == 0.123456789012


def test_completed_backtest_status_converts_signal_execution_delay_numbers() -> None:
    repository = FakeBacktestJobRepository()
    repository.status_views["run-completed"] = BacktestJobStatusView(
        "run-completed", "strategy-v1", "snapshot-1", BacktestJobStatus.COMPLETED,
        actual_period=Period(date(2025, 1, 2), date(2025, 1, 3)), engine_version="v1",
        result_payload={
            "signalExecutionDelay": {
                "median": "69.016666666667",
                "max": "69.016666666667",
                "distribution": ["69.016666666667"],
            },
        },
    )
    with configured_client(repository) as client:
        response = client.get(
            "/backtests/run-completed", headers={"X-Internal-Api-Key": "test-key"}
        )
    assert response.status_code == 200
    assert response.json()["result"]["signalExecutionDelay"] == {
        "median": 69.016666666667,
        "max": 69.016666666667,
        "distribution": [69.016666666667],
    }
