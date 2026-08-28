"""PostgreSQL worker lifecycle integration checks.

Set REFINVEST_TEST_DATABASE_URL to an isolated database with migrations 0001-0004 applied.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from os import getenv
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.application.backtests import BacktestJob, BacktestJobStatus
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.persistence.postgres.backtest_jobs import PostgresBacktestJobRepository
from refinvest.persistence.postgres.dataset_snapshots import insert_dataset_snapshot

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
