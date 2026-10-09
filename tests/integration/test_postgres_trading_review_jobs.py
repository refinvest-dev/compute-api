"""PostgreSQL concurrency and terminal transaction checks.

Set REFINVEST_TEST_DATABASE_URL to an isolated database with migration 0010 applied.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from os import getenv
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.application.trading_review_jobs import (
    SafeTradingReviewFailure,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStatus,
)
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository

DATABASE_URL = getenv("REFINVEST_TEST_DATABASE_URL")
PAYLOAD_KEY = b"K" * 32
pytestmark = pytest.mark.skipif(DATABASE_URL is None, reason="Isolated PostgreSQL test URL absent.")


def _job(logical_id: str) -> TradingReviewJob:
    return TradingReviewJob(
        job_id=str(uuid4()),
        core_logical_job_id=logical_id,
        core_import_session_id=f"session-{logical_id}",
        kind=TradingReviewJobKind.NORMALIZATION_RECONCILIATION,
        request_payload={
            "coreLogicalJobId": logical_id,
            "artifacts": [
                {
                    "role": role,
                    "coreArtifactId": f"{logical_id}-{role}",
                    "rawSha256": "e" * 64,
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
                }
                for role in ("TRADE_HISTORY", "POSITION_HISTORY")
            ],
        },
        request_fingerprint="a" * 64,
        expected_input_hash="b" * 64,
        version_set_hash="c" * 64,
        deletion_generation=0,
        attempt_token="core-attempt-1",
    )


def _delete(job_id: str) -> None:
    assert DATABASE_URL is not None
    with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM trading_review_jobs WHERE job_id = %s", (job_id,))


def _artifact_lease_statuses(job_id: str, attempt: int) -> list[str]:
    assert DATABASE_URL is not None
    with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT status FROM trading_review_artifact_leases
               WHERE job_id = %s AND attempt_number = %s ORDER BY artifact_role""",
            (job_id, attempt),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def test_concurrent_admission_one_job_and_fingerprint_conflict() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    logical_id = str(uuid4())
    key_hash = hashlib.sha256(logical_id.encode()).hexdigest()
    jobs = (_job(logical_id), _job(logical_id))
    accepted_id = ""
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = list(
                pool.map(lambda job: repository.create_idempotently(job, key_hash), jobs)
            )
        assert accepted[0].job_id == accepted[1].job_id
        assert sum(item.newly_created for item in accepted) == 1
        accepted_id = accepted[0].job_id
        conflicting = _job(logical_id)
        conflicting = TradingReviewJob(
            conflicting.job_id,
            conflicting.core_logical_job_id,
            conflicting.core_import_session_id,
            conflicting.kind,
            conflicting.request_payload,
            "d" * 64,
            conflicting.expected_input_hash,
            conflicting.version_set_hash,
            conflicting.deletion_generation,
            conflicting.attempt_token,
        )
        with pytest.raises(TradingReviewIdempotencyConflict):
            repository.create_idempotently(conflicting, key_hash)
    finally:
        if accepted_id:
            _delete(accepted_id)


def test_claim_reclaim_stale_write_and_atomic_terminal_ack_cleanup() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    key_hash = hashlib.sha256(job.job_id.encode()).hexdigest()
    repository.create_idempotently(job, key_hash)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(
                    lambda owner: repository.claim_next(owner, timedelta(seconds=30)),
                    ("worker-a", "worker-b"),
                )
            )
        first = next(claim for claim in claims if claim is not None)
        assert sum(claim is not None for claim in claims) == 1
        assert _artifact_lease_statuses(job.job_id, 1) == ["ACTIVE", "ACTIVE"]
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        reclaimed = repository.claim_next("worker-c", timedelta(seconds=30))
        assert reclaimed is not None
        assert reclaimed.job_id == job.job_id
        assert reclaimed.attempt_number == 2
        assert reclaimed.lease_owner != first.lease_owner
        assert _artifact_lease_statuses(job.job_id, 1) == ["EXPIRED", "EXPIRED"]
        assert _artifact_lease_statuses(job.job_id, 2) == ["ACTIVE", "ACTIVE"]
        payload = b'{"status":"COMPLETED"}'
        payload_hash = hashlib.sha256(payload).hexdigest()
        assert not repository.publish_completed(
            first,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_hash=job.expected_input_hash,
            result_hash="e" * 64,
            retention=timedelta(hours=24),
        )
        assert repository.publish_completed(
            reclaimed,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_hash=job.expected_input_hash,
            result_hash="e" * 64,
            retention=timedelta(hours=24),
        )
        assert _artifact_lease_statuses(job.job_id, 2) == ["CONSUMED", "CONSUMED"]
        view = repository.get(job.job_id)
        assert view is not None and view.status is TradingReviewJobStatus.COMPLETED
        descriptor = repository.get_result_descriptor(job.job_id)
        assert descriptor is not None
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT payload_bytes, encryption_version
                   FROM trading_review_terminal_payloads WHERE payload_id = %s""",
                (descriptor.payload_id,),
            )
            encrypted = cursor.fetchone()
        assert encrypted is not None
        assert bytes(encrypted[0]) != payload
        assert payload not in bytes(encrypted[0])
        assert encrypted[1] == "AES256_GCM_V1"
        assert repository.get_terminal_payload(descriptor.payload_id) == payload
        ack = dict(
            idempotency_key_hash=hashlib.sha256(f"ack-{job.job_id}".encode()).hexdigest(),
            core_aggregate_id=job.core_import_session_id,
            payload_id=descriptor.payload_id,
            payload_sha256=payload_hash,
            terminal_result_hash="e" * 64,
            persisted_at=datetime.now(UTC),
        )
        assert repository.acknowledge(job.job_id, **ack)
        assert repository.acknowledge(job.job_id, **ack)
        assert repository.delete_cleanup_candidates(1) >= 1
        assert repository.get(job.job_id) is None
        assert repository.acknowledge(job.job_id, **ack)
    finally:
        _delete(job.job_id)


def test_pending_cancellation_is_terminal_and_idempotent() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    cancellation_key = hashlib.sha256(f"cancel-{job.job_id}".encode()).hexdigest()
    try:
        first = repository.request_cancellation(job.job_id, cancellation_key)
        second = repository.request_cancellation(job.job_id, cancellation_key)
        assert first is not None and first.status is TradingReviewJobStatus.CANCELLED
        assert second is not None and second.status is TradingReviewJobStatus.CANCELLED
        assert repository.claim_next("worker", timedelta(seconds=30)) is None
    finally:
        _delete(job.job_id)


def test_running_cancellation_blocks_publication_and_expires_safely() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None and claim.job_id == job.job_id
        requested = repository.request_cancellation(
            job.job_id, hashlib.sha256(f"cancel-{job.job_id}".encode()).hexdigest()
        )
        assert requested is not None and requested.cancellation_requested
        payload = b"{}"
        assert not repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_hash=job.expected_input_hash,
            result_hash="f" * 64,
            retention=timedelta(hours=24),
        )
        view = repository.get(job.job_id)
        assert view is not None and view.status is TradingReviewJobStatus.CANCELLED
        assert repository.get_result_descriptor(job.job_id) is None
    finally:
        _delete(job.job_id)


def test_one_active_logical_job_and_expired_lease_never_publishes() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    logical_id = str(uuid4())
    first = _job(logical_id)
    second = _job(logical_id)
    repository.create_idempotently(first, hashlib.sha256(first.job_id.encode()).hexdigest())
    try:
        with pytest.raises(TradingReviewIdempotencyConflict):
            repository.create_idempotently(
                second, hashlib.sha256(second.job_id.encode()).hexdigest()
            )
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None and claim.job_id == first.job_id
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (first.job_id,),
            )
        payload = b"{}"
        assert not repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_hash=first.expected_input_hash,
            result_hash="f" * 64,
            retention=timedelta(hours=24),
        )
        assert repository.get(first.job_id).status is TradingReviewJobStatus.RUNNING  # type: ignore[union-attr]
    finally:
        _delete(first.job_id)


def test_expired_third_attempt_becomes_safe_terminal_failure() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    failure = SafeTradingReviewFailure(
        "TEMPORARY_DEPENDENCY_FAILURE", "Temporary failure.", True, "EXECUTION"
    )
    try:
        for number in (1, 2):
            claim = repository.claim_next(f"worker-{number}", timedelta(seconds=30))
            assert claim is not None and claim.job_id == job.job_id
            assert repository.schedule_retry(claim, timedelta(seconds=30), failure)
            with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE trading_review_jobs
                       SET next_attempt_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                       WHERE job_id = %s""",
                    (job.job_id,),
                )
        third = repository.claim_next("worker-3", timedelta(seconds=30))
        assert third is not None and third.attempt_number == 3
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        assert repository.claim_next("worker-4", timedelta(seconds=30)) is None
        terminal = repository.get(job.job_id)
        assert terminal is not None and terminal.status is TradingReviewJobStatus.FAILED
        assert terminal.failure is not None
        assert terminal.failure.code == "TEMPORARY_DEPENDENCY_FAILURE"
    finally:
        _delete(job.job_id)


def test_unacknowledged_terminal_payload_expires_after_24_hours() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None
        payload = b"{}"
        assert repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_hash=job.expected_input_hash,
            result_hash="f" * 64,
            retention=timedelta(hours=24),
        )
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET terminal_at = CURRENT_TIMESTAMP - INTERVAL '25 hours'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
            cursor.execute(
                """UPDATE trading_review_terminal_payloads
                   SET created_at = CURRENT_TIMESTAMP - INTERVAL '25 hours',
                       expires_at = CURRENT_TIMESTAMP - INTERVAL '1 hour'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        assert repository.get_result_descriptor(job.job_id) is None
        assert repository.delete_cleanup_candidates(1) >= 1
        assert repository.get(job.job_id) is None
    finally:
        _delete(job.job_id)
