"""PostgreSQL concurrency and terminal transaction checks.

Set REFINVEST_TEST_DATABASE_URL to an isolated database with migration 0012 applied.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from os import getenv
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.errors import RaiseException

from refinvest.app.trading_review_result_integrity import (
    ResultIntegrityError,
    canonicalize_result,
    finalize_public_result,
)
from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
    TradingReviewCapacityExceeded,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    normalization_request_fingerprint,
)
from refinvest.persistence.postgres.trading_review_jobs import (
    PostgresTradingReviewJobRepository,
    ResultPayloadLimitExceeded,
)
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
        "resultSchemaVersion": "TRADING_NORMALIZATION_RESULT_V1",
        "resultHashSpecificationVersion": "TRADING_NORMALIZATION_RESULT_HASH_V1",
        "resultDeliveryPolicy": {
            "policyVersion": "RESULT_DELIVERY_V1",
            "mode": "SINGLE_RESOURCE",
            "maxAcceptedPayloadBytes": 52_428_800,
            "acceptedContentEncodings": ["identity"],
        },
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
    claim: ClaimedTradingReviewJob | TradingReviewJob, *, version: str = "v1"
) -> bytes:
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures/trading_review/normalization_identity"
            / version
            / f"result-integrity-{version}.json"
        ).read_text(encoding="utf-8")
    )
    payload = fixture["terminalPayload"]
    identity = claim.request_payload["inputIdentity"]
    payload.update(
        {
            "jobId": claim.job_id,
            "coreLogicalJobId": claim.core_logical_job_id,
            "coreNormalizationRunId": claim.core_normalization_run_id,
            "coreImportSessionId": claim.core_import_session_id,
            "attemptToken": claim.attempt_token,
            "deletionGeneration": claim.deletion_generation,
            "inputSetHash": claim.computed_input_set_hash,
            "targetVersionSet": identity["targetVersionSet"],
        }
    )
    manifest = payload["reconciliationManifest"]
    manifest["inputSetHash"] = claim.computed_input_set_hash
    manifest["versionSetHash"] = identity["targetVersionSet"]["versionSetHash"]
    manifest["sourceArtifactHashesByRole"] = {
        role: item["sha256"] for role, item in identity["artifactsByRole"].items()
    }
    for snapshot in payload["sourceEvidenceSnapshots"]:
        snapshot["coreArtifactId"] = identity["artifactsByRole"][snapshot["artifactRole"]][
            "artifactId"
        ]
    return canonicalize_result(finalize_public_result(payload))


def test_v2_pinned_job_persists_descriptor_resource_and_rejects_other_pair() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    original = _job(str(uuid4()))
    request = {
        **original.request_payload,
        "resultSchemaVersion": "TRADING_NORMALIZATION_RESULT_V2",
        "resultHashSpecificationVersion": "TRADING_NORMALIZATION_RESULT_HASH_V2",
    }
    job = replace(
        original,
        request_payload=request,
        request_fingerprint=normalization_request_fingerprint(request),
        result_schema_version="TRADING_NORMALIZATION_RESULT_V2",
        result_hash_specification_version="TRADING_NORMALIZATION_RESULT_HASH_V2",
    )
    key = hashlib.sha256(job.job_id.encode()).hexdigest()
    repository.create_idempotently(job, key)
    try:
        status = repository.get(job.job_id)
        assert status is not None and status.result_schema_version == job.result_schema_version
        claim = repository.claim_next("v2-worker", timedelta(seconds=30))
        assert claim is not None
        assert claim.result_hash_specification_version == job.result_hash_specification_version
        payload = _terminal_payload(claim, version="v2")
        payload_hash = hashlib.sha256(payload).hexdigest()
        assert repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(payload),
            result_hash=_result_hash(payload),
            retention=timedelta(hours=24),
        )
        descriptor = repository.get_result_descriptor(job.job_id)
        assert descriptor is not None
        assert descriptor.result_schema_version == job.result_schema_version
        assert descriptor.result_hash_specification_version == job.result_hash_specification_version
        assert len(descriptor.section_digests) == 10
        assert repository.get_terminal_payload(descriptor.payload_id) == payload
        v1_request = {
            **request,
            "resultSchemaVersion": "TRADING_NORMALIZATION_RESULT_V1",
            "resultHashSpecificationVersion": "TRADING_NORMALIZATION_RESULT_HASH_V1",
        }
        v1 = replace(
            job,
            request_payload=v1_request,
            request_fingerprint=normalization_request_fingerprint(v1_request),
            result_schema_version="TRADING_NORMALIZATION_RESULT_V1",
            result_hash_specification_version="TRADING_NORMALIZATION_RESULT_HASH_V1",
        )
        with pytest.raises(TradingReviewIdempotencyConflict):
            repository.create_idempotently(v1, key)
    finally:
        _delete(job.job_id)


def test_legacy_v1_replay_rekeys_fingerprint_without_changing_job_pin() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    initial = _job(str(uuid4()))
    job = replace(
        initial,
        request_fingerprint=normalization_request_fingerprint(initial.request_payload),
    )
    key = hashlib.sha256(job.job_id.encode()).hexdigest()
    repository.create_idempotently(job, key)
    try:
        legacy = {
            name: value
            for name, value in job.request_payload.items()
            if name not in {"resultSchemaVersion", "resultHashSpecificationVersion"}
        }
        old_fingerprint = normalization_request_fingerprint(legacy)
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trading_review_job_idempotency SET request_fingerprint = %s "
                "WHERE idempotency_key_hash = %s",
                (old_fingerprint, key),
            )
            cursor.execute(
                "UPDATE trading_review_jobs SET request_fingerprint = %s WHERE job_id = %s",
                (old_fingerprint, job.job_id),
            )
        replay = repository.create_idempotently(job, key)
        assert replay.job_id == job.job_id and not replay.newly_created
        assert replay.result_schema_version == "TRADING_NORMALIZATION_RESULT_V1"
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT request_fingerprint FROM trading_review_job_idempotency "
                "WHERE idempotency_key_hash = %s",
                (key,),
            )
            assert cursor.fetchone()[0] == job.request_fingerprint
    finally:
        _delete(job.job_id)


def _result_hash(payload: bytes) -> str:
    return str(json.loads(payload)["resultHash"])


def _manifest_hash(payload: bytes) -> str:
    return str(json.loads(payload)["reconciliationManifest"]["manifestHash"])


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


def test_pinned_policy_replay_and_change_conflict() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    initial = _job(str(uuid4()))
    initial = replace(
        initial, request_fingerprint=normalization_request_fingerprint(initial.request_payload)
    )
    key = hashlib.sha256(initial.job_id.encode()).hexdigest()
    repository.create_idempotently(initial, key)
    try:
        assert repository.create_idempotently(initial, key).job_id == initial.job_id
        changed_policy = dict(initial.request_payload["resultDeliveryPolicy"])
        changed_policy["maxAcceptedPayloadBytes"] += 1
        changed_payload = {**initial.request_payload, "resultDeliveryPolicy": changed_policy}
        changed = replace(
            initial,
            request_payload=changed_payload,
            request_fingerprint=normalization_request_fingerprint(changed_payload),
        )
        with pytest.raises(TradingReviewIdempotencyConflict):
            repository.create_idempotently(changed, key)
        with pytest.raises(RaiseException):
            with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE trading_review_jobs SET result_delivery_policy = %s WHERE job_id = %s",
                    (json.dumps(changed_policy), initial.job_id),
                )
    finally:
        _delete(initial.job_id)


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
        assert (
            first.request_payload["resultDeliveryPolicy"]
            == job.request_payload["resultDeliveryPolicy"]
        )
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT result_delivery_policy FROM trading_review_jobs WHERE job_id = %s",
                (job.job_id,),
            )
            assert cursor.fetchone()[0] == job.request_payload["resultDeliveryPolicy"]
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
        first_payload = _terminal_payload(first)
        payload = _terminal_payload(reclaimed)
        first_payload_hash = hashlib.sha256(first_payload).hexdigest()
        payload_hash = hashlib.sha256(payload).hexdigest()
        assert not repository.publish_completed(
            first,
            payload=first_payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=first_payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(first_payload),
            result_hash=_result_hash(first_payload),
            retention=timedelta(hours=24),
        )
        assert repository.publish_completed(
            reclaimed,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(payload),
            result_hash=_result_hash(payload),
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
        assert descriptor.reconciliation_manifest_hash == _manifest_hash(payload)
        assert descriptor.result_delivery_policy_version == "RESULT_DELIVERY_V1"
        assert descriptor.content_encoding == "identity"
        assert descriptor.counts == json.loads(payload)["counts"]
        assert descriptor.section_digests == json.loads(payload)["sectionDigests"]
        with pytest.raises(RaiseException):
            with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE trading_review_terminal_payloads SET result_hash = %s
                       WHERE payload_id = %s""",
                    ("0" * 64, descriptor.payload_id),
                )
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
        restarted = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
        assert restarted.get_terminal_payload(descriptor.payload_id) == payload
        assert restarted.get_result_descriptor(job.job_id) == descriptor
        ack = dict(
            idempotency_key_hash=hashlib.sha256(f"ack-{job.job_id}".encode()).hexdigest(),
            core_aggregate_id=job.core_import_session_id,
            payload_id=descriptor.payload_id,
            payload_sha256=payload_hash,
            terminal_result_hash=_result_hash(payload),
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


def test_invalid_digest_cannot_create_partial_terminal_success() -> None:
    assert DATABASE_URL is not None
    repository = PostgresTradingReviewJobRepository(DATABASE_URL, PAYLOAD_KEY)
    job = _job(str(uuid4()))
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None
        document = json.loads(_terminal_payload(claim))
        document["resultHash"] = "0" * 64
        payload = canonicalize_result(document)
        with pytest.raises(ResultIntegrityError, match="RESULT_HASH_MISMATCH"):
            repository.publish_completed(
                claim,
                payload=payload,
                media_type="application/vnd.refinvest.trading-normalization+json",
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                input_set_hash=job.expected_input_set_hash,
                reconciliation_manifest_hash=_manifest_hash(payload),
                result_hash="0" * 64,
                retention=timedelta(hours=24),
            )
        view = repository.get(job.job_id)
        assert view is not None and view.status is TradingReviewJobStatus.RUNNING
        assert repository.get_result_descriptor(job.job_id) is None
        with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM trading_review_terminal_payloads WHERE job_id = %s",
                (job.job_id,),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        _delete(job.job_id)


@pytest.mark.parametrize(
    ("delta", "configured_is_smaller"), [(-1, False), (0, False), (1, False), (-1, True)]
)
def test_exact_payload_limit_and_terminal_publication(
    delta: int, configured_is_smaller: bool
) -> None:
    assert DATABASE_URL is not None
    job = _job(str(uuid4()))
    expected_size = len(_terminal_payload(job))
    boundary = expected_size + delta
    policy = dict(job.request_payload["resultDeliveryPolicy"])
    policy["maxAcceptedPayloadBytes"] = expected_size + 1 if configured_is_smaller else boundary
    job = replace(job, request_payload={**job.request_payload, "resultDeliveryPolicy": policy})
    repository = PostgresTradingReviewJobRepository(
        DATABASE_URL,
        PAYLOAD_KEY,
        max_payload_bytes=boundary if configured_is_smaller else 512 * 1024 * 1024,
    )
    repository.create_idempotently(job, hashlib.sha256(job.job_id.encode()).hexdigest())
    try:
        claim = repository.claim_next("worker", timedelta(seconds=30))
        assert claim is not None
        payload = _terminal_payload(claim)
        assert len(payload) == expected_size

        def publish() -> bool:
            return repository.publish_completed(
                claim,
                payload=payload,
                media_type="application/vnd.refinvest.trading-normalization+json",
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                input_set_hash=job.expected_input_set_hash,
                reconciliation_manifest_hash=_manifest_hash(payload),
                result_hash=_result_hash(payload),
                retention=timedelta(hours=24),
            )

        if delta < 0:
            with pytest.raises(ResultPayloadLimitExceeded):
                publish()
            assert repository.mark_failed(
                claim,
                SafeTradingReviewFailure(
                    "RESULT_PAYLOAD_LIMIT_EXCEEDED",
                    "Normalization result exceeds the configured delivery limit.",
                    False,
                    "PUBLICATION",
                ),
            )
            view = repository.get(job.job_id)
            assert view is not None and view.status is TradingReviewJobStatus.FAILED
            assert view.failure is not None and not view.failure.retryable
            assert view.failure.code == "RESULT_PAYLOAD_LIMIT_EXCEEDED"
            assert repository.get_result_descriptor(job.job_id) is None
            with connect(DATABASE_URL) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM trading_review_terminal_payloads WHERE job_id = %s",
                    (job.job_id,),
                )
                assert cursor.fetchone()[0] == 0
        else:
            assert publish()
            descriptor = repository.get_result_descriptor(job.job_id)
            assert descriptor is not None and descriptor.byte_size == len(payload)
            assert repository.get_terminal_payload(descriptor.payload_id) == payload
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
        payload = _terminal_payload(claim)
        assert not repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(payload),
            result_hash=_result_hash(payload),
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
        payload = _terminal_payload(claim)
        assert not repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_set_hash=first.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(payload),
            result_hash=_result_hash(payload),
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
        payload = _terminal_payload(claim)
        assert repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(payload),
            result_hash=_result_hash(payload),
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
        payload = _terminal_payload(claim)
        payload_hash = hashlib.sha256(payload).hexdigest()
        assert repository.publish_completed(
            claim,
            payload=payload,
            media_type="application/vnd.refinvest.trading-normalization+json",
            payload_sha256=payload_hash,
            input_set_hash=job.expected_input_set_hash,
            reconciliation_manifest_hash=_manifest_hash(payload),
            result_hash=_result_hash(payload),
            retention=timedelta(hours=24),
        )
        descriptor = repository.get_result_descriptor(job.job_id)
        assert descriptor is not None
        ack = dict(
            idempotency_key_hash=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
            core_aggregate_id=job.core_import_session_id,
            payload_id=descriptor.payload_id,
            payload_sha256=payload_hash,
            terminal_result_hash=_result_hash(payload),
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
