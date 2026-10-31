"""TR-ACC-DELETE-009/011/012/013 against an owned, isolated PostgreSQL schema."""

import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.adapters.normalization.process_identity import process_identity
from refinvest.application.normalization_deletion import DeleteNormalizationRuntime
from refinvest.application.trading_review_jobs import (
    TradingReviewCapacityExceeded,
    TradingReviewJobStateConflict,
)
from refinvest.persistence.postgres.normalization_deletion import (
    PostgresNormalizationRuntimeDeletion,
)
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository
from tests.integration.test_postgres_trading_review_jobs import PAYLOAD_KEY, _job

URL = os.getenv("REFINVEST_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(URL is None, reason="Isolated PostgreSQL URL absent")


@pytest.fixture(autouse=True)
def isolated_schema_only():
    assert URL is not None
    with connect(URL) as connection:
        assert connection.info.dbname == "refinvest_integration"
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        assert re.fullmatch(r"deletion_test_[0-9a-f]+", schema)
        connection.execute("TRUNCATE trading_review_jobs, trading_normalization_deletions CASCADE")


def fixture():
    assert URL is not None
    job = _job(str(uuid4()))
    command = DeleteNormalizationRuntime(
        job.core_normalization_run_id,
        job.core_import_session_id,
        job.deletion_generation,
        str(uuid4()),
        1,
    )
    return (
        PostgresTradingReviewJobRepository(URL, PAYLOAD_KEY),
        PostgresNormalizationRuntimeDeletion(URL),
        job,
        command,
    )


def test_unknown_job_is_fenced_before_late_create_and_replay():
    jobs, deletion, job, command = fixture()
    first = deletion.purge(command)
    assert first.verified_at is not None
    assert deletion.purge(command).verified_at == first.verified_at
    with pytest.raises(TradingReviewJobStateConflict):
        jobs.create_idempotently(job, "a" * 64)
    with pytest.raises(TradingReviewJobStateConflict):
        deletion.purge(replace(command, generation=2))
    assert deletion.purge(command).verified_at == first.verified_at


def test_pending_job_purge_frees_capacity_without_touching_other_scope():
    jobs, deletion, job, command = fixture()
    limited = PostgresTradingReviewJobRepository(URL, PAYLOAD_KEY, active_job_limit=1)
    limited.create_idempotently(job, "a" * 64)
    other = _job(str(uuid4()))
    with pytest.raises(TradingReviewCapacityExceeded):
        limited.create_idempotently(other, "b" * 64)
    with pytest.raises(TradingReviewJobStateConflict):
        deletion.purge(replace(command, session_id="other-session"))
    assert jobs.get(job.job_id) is not None
    assert deletion.purge(command).verified_at is not None
    assert jobs.get(job.job_id) is None
    limited.create_idempotently(other, "b" * 64)
    assert jobs.get(other.job_id) is not None
    with pytest.raises(TradingReviewJobStateConflict):
        limited.create_idempotently(job, "a" * 64)


def test_live_child_is_not_absent_even_when_queue_lease_has_expired():
    jobs, deletion, job, command = fixture()
    jobs.create_idempotently(job, "a" * 64)
    claim = jobs.claim_next("test-worker", timedelta(seconds=30))
    assert claim is not None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        birth = process_identity(child.pid)
        assert birth is not None and jobs.bind_process(claim, child.pid, birth)
        with connect(URL) as connection:
            connection.execute(
                """
            UPDATE trading_review_jobs SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL
            '1 second' WHERE job_id=%s
            """,
                (job.job_id,),
            )
        assert deletion.purge(command).verified_at is None
        jobs.release_execution(claim)
        assert deletion.purge(command).verified_at is None
        assert jobs.get(job.job_id) is not None
    finally:
        child.terminate()
        child.wait(timeout=10)
    # Child exit alone does not erase a returned payload still owned by the living parent.
    assert deletion.purge(command).verified_at is None
    jobs.release_execution(claim)
    assert deletion.purge(command).verified_at is not None
    assert jobs.get(job.job_id) is None


def test_unknown_legacy_resource_never_uses_expired_lease_as_absence():
    jobs, deletion, job, command = fixture()
    jobs.create_idempotently(job, "a" * 64)
    assert jobs.claim_next("old-worker", timedelta(seconds=30)) is not None
    with connect(URL) as connection:
        connection.execute("""
            UPDATE trading_normalization_executions SET
            host_name=NULL,parent_pid=NULL,parent_identity=NULL
            """)
        connection.execute(
            "UPDATE trading_review_jobs SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 day'"
        )
    assert deletion.purge(command).verified_at is None
    assert jobs.get(job.job_id) is not None


def test_parent_crash_terminates_tracked_orphan_before_proving_absence():
    jobs, deletion, job, command = fixture()
    jobs.create_idempotently(job, "a" * 64)
    claim = jobs.claim_next("crashed-parent", timedelta(seconds=30))
    assert claim is not None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        birth = process_identity(child.pid)
        assert birth is not None and jobs.bind_process(claim, child.pid, birth)
        with connect(URL) as connection:
            connection.execute(
                "UPDATE trading_normalization_executions SET parent_identity='previous-process'"
            )
        # A termination signal alone does not count as actual absence.
        assert deletion.purge(command).verified_at is None
        child.wait(timeout=10)
        assert deletion.purge(command).verified_at is not None
        assert jobs.get(job.job_id) is None
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


def test_control_receipts_expire_after_completion_but_unknown_execution_never_expires():
    jobs, deletion, job, command = fixture()
    jobs.create_idempotently(job, "a" * 64)
    assert jobs.claim_next("legacy-worker", timedelta(seconds=30)) is not None
    with connect(URL) as connection:
        connection.execute("UPDATE trading_normalization_executions SET host_name=NULL")
    assert deletion.purge(command).verified_at is None
    absent = replace(command, run_id=str(uuid4()), request_id=str(uuid4()))
    assert deletion.purge(absent).verified_at is not None
    assert deletion.delete_expired_receipts(10) == 0
    with connect(URL) as connection:
        connection.execute(
            "UPDATE trading_normalization_deletions "
            "SET requested_at=CURRENT_TIMESTAMP-INTERVAL '181 days'"
        )
        connection.execute(
            """UPDATE trading_normalization_deletions
            SET verified_absent_at=CURRENT_TIMESTAMP-INTERVAL '181 days'
            WHERE core_normalization_run_id=%s""",
            (absent.run_id,),
        )
    assert deletion.delete_expired_receipts(10) == 1
    assert deletion.purge(command).verified_at is None
    with connect(URL) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM trading_normalization_deletion_receipts"
            ).fetchone()[0]
            == 1
        )


def test_delete_before_child_binding_prevents_execution():
    jobs, deletion, job, command = fixture()
    jobs.create_idempotently(job, "a" * 64)
    claim = jobs.claim_next("test-worker", timedelta(seconds=30))
    assert claim is not None
    assert deletion.purge(command).verified_at is None
    assert not jobs.bind_process(claim, os.getpid(), process_identity(os.getpid()))
    jobs.release_execution(claim)
    assert deletion.purge(command).verified_at is not None


@pytest.mark.parametrize("iteration", range(8))
def test_create_and_unknown_job_deletion_are_serialized(iteration):
    jobs, deletion, job, command = fixture()
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=10)
        try:
            jobs.create_idempotently(job, str(uuid4()).replace("-", "").ljust(64, "a"))
        except TradingReviewJobStateConflict:
            return

    def purge():
        barrier.wait(timeout=10)
        return deletion.purge(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = pool.submit(create)
        removed = pool.submit(purge)
        created.result(timeout=20)
        assert removed.result(timeout=20).verified_at is not None
    assert jobs.get(job.job_id) is None
