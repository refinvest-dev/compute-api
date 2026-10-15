"""PostgreSQL concurrency and terminal transaction checks.

Set REFINVEST_TEST_DATABASE_URL to an isolated database with migration 0012 applied.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from os import getenv
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
    TradingReviewCapacityExceeded,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
)
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    simple_long_request,
)

DATABASE_URL = getenv("REFINVEST_TEST_DATABASE_URL")
PAYLOAD_KEY = b"K" * 32
pytestmark = pytest.mark.skipif(DATABASE_URL is None, reason="Isolated PostgreSQL test URL absent.")


def _job(logical_id: str) -> TradingReviewJob:
    identity = normalization_identity_for(
        simple_long_request(), book_id=f"book-{logical_id}", session_id=f"session-{logical_id}"
    )
    artifacts = identity["artifactsByRole"]
    assert isinstance(artifacts, dict)
    for role in ("TRADE_HISTORY", "POSITION_HISTORY"):
        artifacts[role] = {"artifactId": f"{logical_id}-{role}", "sha256": "e" * 64}
    input_set_hash = input_set_hash_for(identity)
    request_payload: dict[str, object] = {
        "coreLogicalJobId": logical_id,
        "coreNormalizationRunId": f"run-{logical_id}",
        "coreImportSessionId": f"session-{logical_id}",
        "inputIdentity": identity,
        "expectedInputSetHash": input_set_hash,
        "retryPolicyVersion": "POLICY_V1",
        "artifactAccessGrants": [
            {
                "role": role,
                "coreArtifactId": f"{logical_id}-{role}",
                "rawSha256": "e" * 64,
                "byteSize": 100,
                "contentType": "text/csv",
                "accessUri": f"https://internal.example/{uuid4()}",
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
            }
            for role in ("TRADE_HISTORY", "POSITION_HISTORY")
        ],
    }
    return TradingReviewJob(
        job_id=str(uuid4()),
        core_logical_job_id=logical_id,
        core_import_session_id=f"session-{logical_id}",
        core_normalization_run_id=f"run-{logical_id}",
        kind=TradingReviewJobKind.NORMALIZATION_RECONCILIATION,
        request_payload=request_payload,
        request_fingerprint="a" * 64,
        expected_input_set_hash=input_set_hash,
        computed_input_set_hash=input_set_hash,
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        retry_policy_version="POLICY_V1",
        version_set_hash="c" * 64,
        deletion_generation=0,
        attempt_token="core-attempt-1",
    )


def _terminal_payload(
    claim: ClaimedTradingReviewJob, *, result_hash: str, manifest_hash: str = "d" * 64
) -> bytes:
    return json.dumps(
        {
            "jobId": claim.job_id,
            "coreNormalizationRunId": claim.core_normalization_run_id,
            "attemptToken": claim.attempt_token,
            "deletionGeneration": claim.deletion_generation,
            "inputSetHash": claim.computed_input_set_hash,
            "hashSpecificationVersion": claim.hash_specification_version,
            "reconciliationManifest": {"manifestHash": manifest_hash},
            "resultHash": result_hash,
        },
        separators=(",", ":"),
    ).encode()


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


def _renew(
    repository: PostgresTradingReviewJobRepository, job: TradingReviewJob, mode: str
) -> None:
    view = repository.get(job.job_id)
    assert view is not None and view.grant_action == mode
    next_token = view.attempt_token if mode == "REPLACE_QUEUED_GRANTS" else str(uuid4())
    grants = [
        {
            **grant,
            "accessUri": f"https://internal.example/{uuid4()}",
            "expiresAt": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
        }
        for grant in job.request_payload["artifactAccessGrants"]  # type: ignore[union-attr]
    ]
    receipt = repository.update_grants(
        job.job_id,
        hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        {
            "mode": mode,
            "expectedGrantRevision": view.grant_revision,
            "expectedAttemptToken": view.attempt_token,
            "nextAttemptToken": next_token,
            "expectedInputSetHash": job.expected_input_set_hash,
            "versionSetHash": job.version_set_hash,
            "deletionGeneration": job.deletion_generation,
            "artifactAccessGrants": grants,
        },
    )
    assert receipt is not None and receipt.attempt_token == next_token


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
        conflicting = replace(conflicting, request_fingerprint="d" * 64)
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
        assert first.core_normalization_run_id == job.core_normalization_run_id
        assert first.request_payload["inputIdentity"] == job.request_payload["inputIdentity"]
        assert first.computed_input_set_hash == job.computed_input_set_hash
        assert first.retry_policy_version == job.retry_policy_version
        assert _artifact_lease_statuses(job.job_id, 1) == ["ACTIVE", "ACTIVE"]
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        assert repository.claim_next("worker-c", timedelta(seconds=30)) is None
        waiting = repository.get(job.job_id)
        assert waiting is not None and waiting.grant_action == "START_RETRY_ATTEMPT"
        _renew(repository, job, "START_RETRY_ATTEMPT")
        reclaimed = repository.claim_next("worker-c", timedelta(seconds=30))
        assert reclaimed is not None
        assert reclaimed.job_id == job.job_id
        assert reclaimed.attempt_number == 2
        assert reclaimed.lease_owner != first.lease_owner
        assert _artifact_lease_statuses(job.job_id, 1) == ["EXPIRED", "EXPIRED"]
        assert _artifact_lease_statuses(job.job_id, 2) == ["ACTIVE", "ACTIVE"]
        first_payload = _terminal_payload(first, result_hash="e" * 64)
        payload = _terminal_payload(reclaimed, result_hash="e" * 64)
        first_payload_hash = hashlib.sha256(first_payload).hexdigest()
        payload_hash = hashlib.sha256(payload).hexdigest()
        assert not repository.publish_completed(
            first,
            payload=first_payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=first_payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash="d" * 64,
            result_hash="e" * 64,
            retention=timedelta(hours=24),
        )
        assert repository.publish_completed(
            reclaimed,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash="d" * 64,
            result_hash="e" * 64,
            retention=timedelta(hours=24),
        )
        assert _artifact_lease_statuses(job.job_id, 2) == ["CONSUMED", "CONSUMED"]
        view = repository.get(job.job_id)
        assert view is not None and view.status is TradingReviewJobStatus.COMPLETED
        descriptor = repository.get_result_descriptor(job.job_id)
        assert descriptor is not None
        assert descriptor.core_normalization_run_id == job.core_normalization_run_id
        assert descriptor.input_set_hash == job.computed_input_set_hash
        assert (
            descriptor.target_version_set
            == job.request_payload["inputIdentity"]["targetVersionSet"]
        )
        assert descriptor.reconciliation_manifest_hash == "d" * 64
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
            attempt_token=descriptor.attempt_token,
            deletion_generation=descriptor.deletion_generation,
            disposition="PERSISTED",
            recorded_at=datetime.now(UTC),
            current_deletion_generation=None,
        )
        assert repository.acknowledge(job.job_id, **ack)
        assert repository.acknowledge(job.job_id, **ack)
        assert repository.delete_cleanup_candidates(1) == 0
        assert repository.get(job.job_id) is not None
        assert repository.get_terminal_payload(descriptor.payload_id) is None
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
        payload = _terminal_payload(claim, result_hash="f" * 64)
        assert not repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash="d" * 64,
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
        payload = _terminal_payload(claim, result_hash="f" * 64)
        assert not repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_set_hash=first.expected_input_set_hash,
            reconciliation_manifest_hash="d" * 64,
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
            assert repository.claim_next("stale-worker", timedelta(seconds=30)) is None
            _renew(repository, job, "START_RETRY_ATTEMPT")
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
        assert terminal.failure.code == "GRANT_REFRESH_DEADLINE_EXCEEDED"
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
        payload = _terminal_payload(claim, result_hash="f" * 64)
        assert repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash="d" * 64,
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


def test_queued_expiry_requires_explicit_grant_update_and_replay_does_not_replace() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    key = hashlib.sha256(job.job_id.encode()).hexdigest()
    repository.create_idempotently(job, key)
    try:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_grant_revisions
                   SET grant_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        first = repository.get(job.job_id)
        assert first is not None and first.status is TradingReviewJobStatus.PENDING
        assert first.grant_action == "REPLACE_QUEUED_GRANTS"
        assert first.grant_refresh_deadline_at is not None
        assert repository.claim_next("worker", timedelta(seconds=30)) is None
        replay = repository.create_idempotently(_job(job.core_logical_job_id), key)
        assert replay.job_id == job.job_id and not replay.newly_created
        again = repository.get(job.job_id)
        assert (
            again is not None and again.grant_refresh_deadline_at == first.grant_refresh_deadline_at
        )
        _renew(repository, job, "REPLACE_QUEUED_GRANTS")
        ready = repository.get(job.job_id)
        assert ready is not None and ready.grant_revision == 1
        assert ready.attempt_token == job.attempt_token
        assert ready.grant_action == "NONE"
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None and claim.grant_revision == 1
    finally:
        _delete(job.job_id)


def test_grant_update_receipt_replay_conflict_and_live_lease_rejection() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None
        update_key = hashlib.sha256(str(uuid4()).encode()).hexdigest()
        body = {
            "mode": "START_RETRY_ATTEMPT",
            "expectedGrantRevision": 0,
            "expectedAttemptToken": job.attempt_token,
            "nextAttemptToken": str(uuid4()),
            "expectedInputSetHash": job.expected_input_set_hash,
            "versionSetHash": job.version_set_hash,
            "deletionGeneration": 0,
            "artifactAccessGrants": [
                {**grant, "accessUri": f"https://internal.example/{uuid4()}"}
                for grant in job.request_payload["artifactAccessGrants"]  # type: ignore[union-attr]
            ],
        }
        with pytest.raises(TradingReviewJobStateConflict):
            repository.update_grants(job.job_id, update_key, body)
        assert repository.schedule_retry(
            claim,
            timedelta(seconds=1),
            SafeTradingReviewFailure(
                "TEMPORARY_DEPENDENCY_FAILURE", "Retry.", True, "ARTIFACT_FETCH"
            ),
        )
        receipt = repository.update_grants(job.job_id, update_key, body)
        assert receipt is not None and receipt.grant_revision == 1
        assert repository.update_grants(job.job_id, update_key, body) == receipt
        with pytest.raises(TradingReviewIdempotencyConflict):
            repository.update_grants(
                job.job_id, update_key, {**body, "nextAttemptToken": str(uuid4())}
            )
        with pytest.raises(TradingReviewJobStateConflict):
            repository.update_grants(
                job.job_id, hashlib.sha256(str(uuid4()).encode()).hexdigest(), body
            )
    finally:
        _delete(job.job_id)


def test_dedicated_capacity_rejects_new_job_without_idempotency_row() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY, active_job_limit=1)
    first = _job(str(uuid4()))
    second = _job(str(uuid4()))
    first_key = hashlib.sha256(first.job_id.encode()).hexdigest()
    second_key = hashlib.sha256(second.job_id.encode()).hexdigest()
    repository.create_idempotently(first, first_key)
    try:
        with pytest.raises(TradingReviewCapacityExceeded):
            repository.create_idempotently(second, second_key)
        assert not repository.create_idempotently(first, first_key).newly_created
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT COUNT(*) FROM trading_review_job_idempotency
                   WHERE idempotency_key_hash = %s""",
                (second_key,),
            )
            assert cursor.fetchone()[0] == 0
        repository.request_cancellation(
            first.job_id, hashlib.sha256(str(uuid4()).encode()).hexdigest()
        )
        assert repository.create_idempotently(second, second_key).newly_created
    finally:
        _delete(first.job_id)
        _delete(second.job_id)


def test_grant_wait_deadline_is_fixed_and_terminal_failure_cannot_be_revived() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_grant_revisions
                   SET grant_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        waiting = repository.get(job.job_id)
        assert waiting is not None and waiting.grant_action == "REPLACE_QUEUED_GRANTS"
        first_deadline = waiting.grant_refresh_deadline_at
        assert first_deadline is not None
        again = repository.get(job.job_id)
        assert again is not None and again.grant_refresh_deadline_at == first_deadline
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET grant_refresh_deadline_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                   WHERE job_id = %s""",
                (job.job_id,),
            )
        terminal = repository.get(job.job_id)
        assert terminal is not None and terminal.status is TradingReviewJobStatus.FAILED
        assert terminal.failure is not None
        assert terminal.failure.code == "GRANT_REFRESH_DEADLINE_EXCEEDED"
        assert repository.claim_next("worker", timedelta(seconds=30)) is None
        with pytest.raises(TradingReviewJobStateConflict):
            repository.update_grants(
                job.job_id,
                hashlib.sha256(str(uuid4()).encode()).hexdigest(),
                {
                    "mode": "REPLACE_QUEUED_GRANTS",
                    "expectedGrantRevision": 0,
                    "expectedAttemptToken": job.attempt_token,
                    "nextAttemptToken": job.attempt_token,
                    "expectedInputSetHash": job.expected_input_set_hash,
                    "versionSetHash": job.version_set_hash,
                    "deletionGeneration": 0,
                    "artifactAccessGrants": job.request_payload["artifactAccessGrants"],
                },
            )
    finally:
        _delete(job.job_id)


def test_acknowledgement_requires_final_token_and_stale_discard_generation() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None
        payload = _terminal_payload(claim, result_hash="f" * 64)
        payload_hash = hashlib.sha256(payload).hexdigest()
        assert repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash="d" * 64,
            result_hash="f" * 64,
            retention=timedelta(hours=24),
        )
        descriptor = repository.get_result_descriptor(job.job_id)
        assert descriptor is not None
        ack = dict(
            idempotency_key_hash=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
            core_aggregate_id=job.core_import_session_id,
            payload_id=descriptor.payload_id,
            payload_sha256=payload_hash,
            terminal_result_hash="f" * 64,
            attempt_token=descriptor.attempt_token,
            deletion_generation=descriptor.deletion_generation,
            disposition="DISCARDED_STALE",
            recorded_at=datetime.now(UTC),
            current_deletion_generation=1,
        )
        with pytest.raises(TradingReviewJobStateConflict):
            repository.acknowledge(job.job_id, **{**ack, "attempt_token": "stale"})
        with pytest.raises(TradingReviewJobStateConflict):
            repository.acknowledge(job.job_id, **{**ack, "current_deletion_generation": 0})
        assert repository.acknowledge(job.job_id, **ack)
        assert repository.acknowledge(job.job_id, **ack)
        repository.delete_cleanup_candidates(10)
        assert repository.get_terminal_payload(descriptor.payload_id) is None
        assert repository.get(job.job_id) is not None
    finally:
        _delete(job.job_id)
