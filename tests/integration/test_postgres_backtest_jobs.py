"""PostgreSQL worker lifecycle integration checks.

Set REFINVEST_TEST_DATABASE_URL to an isolated database with migrations 0001-0007 applied.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from os import getenv
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.application.backtest_executor import DailyBarBacktestExecutor
from refinvest.application.backtest_worker_host import BacktestWorkerHost
from refinvest.application.backtests import (
    BacktestIdempotencyConflict,
    BacktestJob,
    BacktestJobStatus,
)
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.engine.validation.models import Period
from refinvest.persistence.object_storage.snapshot_artifacts import (
    FilesystemSnapshotExecutionDataLoader,
)
from refinvest.persistence.postgres.backtest_jobs import PostgresBacktestJobRepository
from refinvest.persistence.postgres.dataset_snapshots import (
    PostgresDatasetSnapshotRepository,
    insert_dataset_snapshot,
)

DATABASE_URL = getenv("REFINVEST_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="REFINVEST_TEST_DATABASE_URL is not configured.",
)


def test_claim_is_exclusive_and_stale_lease_is_reclaimed() -> None:
    assert DATABASE_URL is not None
    run_id = str(uuid4())
    snapshot_id = f"snapshot-{uuid4()}"
    repository = PostgresBacktestJobRepository(DATABASE_URL)
    insert_dataset_snapshot(
        DATABASE_URL,
        DatasetSnapshot(snapshot_id, datetime.now(UTC), "test", {}, "adjusted", "test/path"),
    )
    repository.create(
        BacktestJob(run_id, None, snapshot_id, BacktestJobStatus.PENDING, {"test": True})
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(
                executor.map(
                    lambda worker_id: repository.claim_next(worker_id, timedelta(seconds=30)),
                    ("worker-a", "worker-b"),
                )
            )
        claimed = [claim for claim in claims if claim is not None]
        assert len(claimed) == 1
        first_claim = claimed[0]
        assert first_claim.dataset_snapshot_id == snapshot_id
        assert repository.heartbeat(run_id, first_claim.worker_id, timedelta(seconds=30))

        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE compute_backtest_jobs
                SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE run_id = %s
                """,
                (run_id,),
            )

        reclaimed = repository.claim_next("worker-c", timedelta(seconds=30))
        assert reclaimed is not None
        assert reclaimed.run_id == run_id
        assert reclaimed.dataset_snapshot_id == snapshot_id
        assert reclaimed.attempt_count == 2
    finally:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM compute_backtest_jobs WHERE run_id = %s", (run_id,))
            cursor.execute(
                "DELETE FROM compute_dataset_snapshots WHERE snapshot_id = %s", (snapshot_id,)
            )


def test_idempotent_submission_is_exclusive_and_rejects_different_payload() -> None:
    assert DATABASE_URL is not None
    repository = PostgresBacktestJobRepository(DATABASE_URL)
    key_hash = hashlib.sha256(str(uuid4()).encode()).hexdigest()
    run_ids = (str(uuid4()), str(uuid4()))

    def submit(run_id: str) -> str:
        return repository.create_idempotently(
            BacktestJob(run_id, "strategy-v1", None, BacktestJobStatus.PENDING, {"value": "1"}),
            key_hash,
            "fingerprint-1",
        ).run_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted = list(executor.map(submit, run_ids))
        assert accepted == [accepted[0], accepted[0]]

        with pytest.raises(BacktestIdempotencyConflict):
            repository.create_idempotently(
                BacktestJob(str(uuid4()), "strategy-v1", None, BacktestJobStatus.PENDING, {}),
                key_hash,
                "fingerprint-2",
            )
    finally:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM compute_backtest_jobs WHERE idempotency_key_hash = %s", (key_hash,)
            )


def test_lease_owner_persists_completed_result() -> None:
    assert DATABASE_URL is not None
    run_id = str(uuid4())
    snapshot_id = f"snapshot-{uuid4()}"
    repository = PostgresBacktestJobRepository(DATABASE_URL)
    insert_dataset_snapshot(
        DATABASE_URL,
        DatasetSnapshot(snapshot_id, datetime.now(UTC), "test", {}, "adjusted", "test/path"),
    )
    repository.create(BacktestJob(run_id, None, snapshot_id, BacktestJobStatus.PENDING, {}))
    try:
        claim = repository.claim_next("worker-a", timedelta(seconds=30))
        assert claim is not None
        actual_period = Period(datetime(2025, 1, 2).date(), datetime(2025, 1, 3).date())
        assert repository.mark_completed(
            run_id,
            "worker-a",
            {"metrics": {"totalReturn": "0.12345678901234"}},
            actual_period,
            "v1",
        )
        assert not repository.mark_completed(run_id, "worker-b", {}, actual_period, "v2")
        status = repository.get(run_id)
        assert status is not None
        assert status.status is BacktestJobStatus.COMPLETED
        assert status.actual_period == actual_period
        assert status.engine_version == "v1"
        assert status.result_payload == {"metrics": {"totalReturn": "0.12345678901234"}}
    finally:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM compute_backtest_jobs WHERE run_id = %s", (run_id,))
            cursor.execute(
                "DELETE FROM compute_dataset_snapshots WHERE snapshot_id = %s", (snapshot_id,)
            )


def test_cleanup_removes_only_expired_terminal_jobs() -> None:
    assert DATABASE_URL is not None
    repository = PostgresBacktestJobRepository(DATABASE_URL)
    expired_completed = str(uuid4())
    expired_failed = str(uuid4())
    fresh_completed = str(uuid4())
    pending = str(uuid4())
    run_ids = (expired_completed, expired_failed, fresh_completed, pending)
    for run_id in run_ids:
        repository.create(BacktestJob(run_id, None, None, BacktestJobStatus.PENDING, {}))
    with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE compute_backtest_jobs
            SET status = 'COMPLETED', terminal_at = CURRENT_TIMESTAMP - INTERVAL '25 hours',
                completed_at = CURRENT_TIMESTAMP - INTERVAL '25 hours',
                result_payload = '{}'::jsonb, actual_period_start = DATE '2025-01-01',
                actual_period_end = DATE '2025-01-01', engine_version = 'test'
            WHERE run_id = %s
            """,
            (expired_completed,),
        )
        cursor.execute(
            """
            UPDATE compute_backtest_jobs
            SET status = 'FAILED', terminal_at = CURRENT_TIMESTAMP - INTERVAL '25 hours'
            WHERE run_id = %s
            """,
            (expired_failed,),
        )
        cursor.execute(
            """
            UPDATE compute_backtest_jobs
            SET status = 'COMPLETED', terminal_at = CURRENT_TIMESTAMP - INTERVAL '23 hours',
                completed_at = CURRENT_TIMESTAMP - INTERVAL '23 hours',
                result_payload = '{}'::jsonb, actual_period_start = DATE '2025-01-01',
                actual_period_end = DATE '2025-01-01', engine_version = 'test'
            WHERE run_id = %s
            """,
            (fresh_completed,),
        )
    try:
        assert repository.delete_expired_terminal_jobs(timedelta(hours=24), batch_size=1) == 1
        assert repository.delete_expired_terminal_jobs(timedelta(hours=24), batch_size=1) == 1
        assert repository.delete_expired_terminal_jobs(timedelta(hours=24), batch_size=1) == 0
        assert repository.get(expired_completed) is None
        assert repository.get(expired_failed) is None
        assert repository.get(fresh_completed) is not None
        assert repository.get(pending) is not None
    finally:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM compute_backtest_jobs WHERE run_id = ANY(%s)", (run_ids,))


def test_worker_completes_a_manifest_backed_zero_trade_backtest(tmp_path: Path) -> None:
    assert DATABASE_URL is not None
    run_id = str(uuid4())
    snapshot_id = f"snapshot-{uuid4()}"
    _write_zero_trade_artifacts(tmp_path, snapshot_id)
    snapshot = DatasetSnapshot(
        snapshot_id,
        datetime.now(UTC),
        "test",
        {},
        "adjusted",
        str(tmp_path),
        "test-calendar",
        "v1",
        "calendar-provenance-hash",
    )
    jobs = PostgresBacktestJobRepository(DATABASE_URL)
    insert_dataset_snapshot(DATABASE_URL, snapshot)
    jobs.create(
        BacktestJob(
            run_id,
            None,
            snapshot_id,
            BacktestJobStatus.PENDING,
            _zero_trade_request_payload(snapshot_id),
        )
    )
    try:
        worker = BacktestWorkerHost(
            "worker-a",
            timedelta(seconds=30),
            timedelta(seconds=10),
            jobs,
            PostgresDatasetSnapshotRepository(DATABASE_URL),
            DailyBarBacktestExecutor(FilesystemSnapshotExecutionDataLoader(), "integration-v1"),
        )

        assert worker.process_once()
        status = jobs.get(run_id)

        assert status is not None
        assert status.status is BacktestJobStatus.COMPLETED
        assert status.engine_version == "integration-v1"
        assert status.result_payload is not None
        assert status.result_payload["sampleSizeWarning"] == "ZERO"
    finally:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM compute_backtest_jobs WHERE run_id = %s", (run_id,))
            cursor.execute(
                "DELETE FROM compute_dataset_snapshots WHERE snapshot_id = %s", (snapshot_id,)
            )


def _write_zero_trade_artifacts(path: Path, snapshot_id: str) -> None:
    calendar_content = json.dumps(
        {
            "provenance": {
                "source": "test-calendar",
                "version": "v1",
                "contentHash": "calendar-provenance-hash",
            },
            "sessions": [
                {
                    "asset": "QQQ",
                    "sessionDate": "2025-01-02",
                    "state": "EXPECTED",
                    "completedAt": "2025-01-02T21:00:00Z",
                },
                {
                    "asset": "QQQ",
                    "sessionDate": "2025-01-03",
                    "state": "EXPECTED",
                    "completedAt": "2025-01-03T21:00:00Z",
                },
            ],
        },
        separators=(",", ":"),
    ).encode()
    daily_bars_content = (
        b'{"asset":"QQQ","sessionDate":"2025-01-02","open":"100",'
        b'"high":"100","low":"100","close":"100","volume":1}\n'
        b'{"asset":"QQQ","sessionDate":"2025-01-03","open":"100",'
        b'"high":"100","low":"100","close":"100","volume":1}\n'
    )
    (path / "calendar.json").write_bytes(calendar_content)
    (path / "daily-bars.ndjson").write_bytes(daily_bars_content)
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "snapshotId": snapshot_id,
                "corporateActionsApplied": True,
                "pointInTimeValidationPassed": True,
                "calendar": {"path": "calendar.json", "sha256": _sha256(calendar_content)},
                "dailyBars": {"path": "daily-bars.ndjson", "sha256": _sha256(daily_bars_content)},
            }
        ),
        encoding="utf-8",
    )


def _zero_trade_request_payload(snapshot_id: str) -> dict[str, object]:
    return {
        "strategyVersionId": None,
        "strategyVersion": {
            "primarySignalAsset": "QQQ",
            "conditions": [
                {
                    "operator": "LT",
                    "logicalCombinator": None,
                    "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": None},
                    "operandB": "0",
                }
            ],
            "executionAsset": "QQQ",
            "lag": 0,
            "exit": {"holdingSignalSessions": 1},
            "positionPolicy": None,
        },
        "feeModel": {"commission": "0", "slippage": "0"},
        "period": {"start": "2025-01-02", "end": "2025-01-03"},
        "datasetSnapshotId": snapshot_id,
    }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
