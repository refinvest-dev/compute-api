"""Analytics V1 durable create, lease fencing, delivery and retention on PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from os import getenv
from uuid import uuid4

import pytest
from psycopg import connect

from refinvest.application.analytics_jobs import AnalyticsJobConflict, AnalyticsJobUnavailable
from refinvest.application.trading_review_jobs import (
    TradingReviewCapacityExceeded,
    hash_idempotency_key,
)
from refinvest.contracts.analytics.identity import create_fingerprint
from refinvest.persistence.postgres.trading_analytics_jobs import PostgresAnalyticsJobRepository
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository
from tests.integration.test_postgres_trading_review_jobs import _job as _normalization_job
from tests.test_trading_analytics_runtime import FIXTURE, _request, _validate

DATABASE_URL = getenv("REFINVEST_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(DATABASE_URL is None, reason="Isolated PostgreSQL test URL absent.")


def _repo() -> PostgresAnalyticsJobRepository:
    assert DATABASE_URL is not None
    return PostgresAnalyticsJobRepository(
        DATABASE_URL, b"A" * 32, active_job_limit=1000, max_payload_bytes=1_000_000
    )


def _create(
    repo: PostgresAnalyticsJobRepository, request: dict[str, object], key: str
) -> tuple[dict[str, object], bool]:
    return repo.create(request, hash_idempotency_key(key), create_fingerprint(request))


def test_migration_concurrent_create_claim_and_restart_fencing() -> None:
    assert DATABASE_URL is not None
    with connect(DATABASE_URL) as connection:
        assert connection.execute("SELECT to_regclass('trading_analytics_jobs')").fetchone()[0]
    repo = _repo()
    request = _request()
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        creates = list(pool.map(lambda _: _create(repo, request, key), range(2)))
    assert {created for _, created in creates} == {True, False}
    job_id = str(creates[0][0]["jobId"])
    assert {str(status["jobId"]) for status, _ in creates} == {job_id}
    _validate("TradingAnalyticsJobAccepted", next(status for status, created in creates if created))
    replay = next(status for status, created in creates if not created)
    _validate("TradingAnalyticsJobStatusResponse", replay)
    changed = dict(request, coreAnalysisRunId=str(uuid4()))
    with pytest.raises(AnalyticsJobConflict, match="IDEMPOTENCY_CONFLICT"):
        _create(repo, changed, key)
    with pytest.raises(AnalyticsJobConflict, match="IDEMPOTENCY_CONFLICT"):
        _create(repo, request, str(uuid4()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(lambda _: repo.claim_next(f"worker-{_}", timedelta(seconds=60)), range(2))
        )
    assert sum(item is not None for item in claims) == 1
    first = next(item for item in claims if item is not None)
    assert first["jobId"] == job_id
    assert repo.heartbeat(first)
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE trading_analytics_jobs "
            "SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE job_id=%s",
            (job_id,),
        )
    restarted = repo.claim_next("restarted-worker", timedelta(seconds=60))
    assert restarted is not None and restarted["leaseGeneration"] != first["leaseGeneration"]
    assert not repo.heartbeat(first)
    assert not repo.fail_attempt(first, "INPUT_STREAM_ABORTED", retryable=True)
    assert repo.fail_attempt(restarted, "INPUT_STREAM_ABORTED", retryable=True)
    status = repo.get(job_id)
    assert status is not None and status["status"] == "PENDING" and status["retryAvailable"]
    _validate("TradingAnalyticsJobStatusResponse", status)
    assert repo.claim_next("other-worker") is None
    update = {
        "mode": "START_RETRY_ATTEMPT",
        "expectedGrantRevision": 0,
        "expectedAttemptToken": status["attemptToken"],
        "nextAttemptToken": str(uuid4()),
        "expectedInputHash": status["expectedInputHash"],
        "versionSetHash": request["targetVersionSet"]["versionSetHash"],
        "deletionGeneration": status["deletionGeneration"],
        "expectedRetryCutoffAt": status["retryCutoffAt"],
        "ledgerRevisionContent": request["ledgerRevisionContent"],
        "reconciliationManifest": request["reconciliationManifest"],
    }
    receipt = repo.update_grants(job_id, update, hash_idempotency_key(str(uuid4())))
    assert receipt["attempt"] == 2
    retry_claim = repo.claim_next("retry-worker")
    assert retry_claim is not None and retry_claim["attempt"] == 2
    assert retry_claim["attemptToken"] == update["nextAttemptToken"]
    assert repo.fail_attempt(retry_claim, "INPUT_SCHEMA_UNSUPPORTED", retryable=False)
    terminal = repo.get(job_id)
    assert terminal is not None and terminal["status"] == "FAILED"
    _validate("TradingAnalyticsJobStatusResponse", terminal)
    assert repo.claim_next("later") is None


def test_terminal_descriptor_payload_ack_and_cleanup() -> None:
    assert DATABASE_URL is not None
    repo = _repo()
    request = _request()
    status, created = _create(repo, request, str(uuid4()))
    assert created
    job_id = str(status["jobId"])
    claim = repo.claim_next("result-worker")
    assert claim is not None and claim["jobId"] == job_id
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    vector = json.loads(FIXTURE.with_name("result-integrity-v1.json").read_text())
    payload = {
        "createdAt": now,
        "terminalAt": now,
        "inputHash": request["expectedInputHash"],
        "inputHashSpecificationVersion": request["inputHashSpecificationVersion"],
        "analysisConfigHash": request["analysisConfigHash"],
        "targetVersionSet": request["targetVersionSet"],
        "resultSchemaVersion": request["resultSchemaVersion"],
        "resultHashSpecificationVersion": request["resultHashSpecificationVersion"],
        "counts": vector["counts"],
        "sectionDigests": vector["sectionDigests"],
        "evidenceHash": vector["evidenceHash"],
        "resultHash": vector["expectedResultHash"],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    assert repo.publish_completed(claim, payload, body)
    assert not repo.publish_completed(claim, payload, body)
    completed = repo.get(job_id)
    assert completed is not None and completed["status"] == "COMPLETED"
    _validate("TradingAnalyticsJobStatusResponse", completed)
    descriptor = repo.result_descriptor(job_id)
    assert descriptor is not None
    _validate("AnalyticsTerminalResultDescriptor", descriptor)
    assert descriptor["byteSize"] == len(body)
    assert descriptor["payloadSha256"] == hashlib.sha256(body).hexdigest()
    assert repo.payload_bytes(str(descriptor["payloadId"])) == body
    ack = {
        "coreAnalysisRunId": request["coreAnalysisRunId"],
        "payloadId": descriptor["payloadId"],
        "payloadSha256": descriptor["payloadSha256"],
        "terminalResultHash": descriptor["resultHash"],
        "attemptToken": claim["attemptToken"],
        "deletionGeneration": claim["deletionGeneration"],
        "disposition": "PERSISTED",
        "persistedAt": now,
    }
    repo.acknowledge(job_id, ack)
    repo.acknowledge(job_id, ack)
    with pytest.raises(AnalyticsJobConflict, match="ACK_CONFLICT"):
        repo.acknowledge(job_id, dict(ack, terminalResultHash="c" * 64))
    assert repo.payload_bytes(str(descriptor["payloadId"])) is None
    assert repo.result_descriptor(job_id) is None
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE trading_analytics_payloads "
            "SET created_at=CURRENT_TIMESTAMP-INTERVAL '25 hours', "
            "expires_at=CURRENT_TIMESTAMP-INTERVAL '1 hour' WHERE payload_id=%s",
            (descriptor["payloadId"],),
        )
        connection.execute(
            "UPDATE trading_analytics_jobs "
            "SET terminal_at=CURRENT_TIMESTAMP-INTERVAL '25 hours' WHERE job_id=%s",
            (job_id,),
        )
    with pytest.raises(AnalyticsJobUnavailable, match="RESULT_PAYLOAD_EXPIRED"):
        repo.payload_bytes(str(descriptor["payloadId"]))
    _, jobs = repo.cleanup()
    assert jobs >= 1
    assert repo.get(job_id) is None


def test_normalization_and_analytics_share_admission_capacity() -> None:
    assert DATABASE_URL is not None
    repo = _repo()
    request = _request()
    accepted, _ = _create(repo, request, str(uuid4()))
    job_id = str(accepted["jobId"])
    try:
        with connect(DATABASE_URL) as connection:
            active = connection.execute(
                "SELECT (SELECT COUNT(*) FROM trading_review_jobs "
                "WHERE status IN ('PENDING','RUNNING')) + "
                "(SELECT COUNT(*) FROM trading_analytics_jobs "
                "WHERE status IN ('PENDING','RUNNING'))"
            ).fetchone()[0]
        normalization = PostgresTradingReviewJobRepository(
            DATABASE_URL, b"A" * 32, active_job_limit=active
        )
        with pytest.raises(TradingReviewCapacityExceeded):
            normalization.create_idempotently(
                _normalization_job(str(uuid4())), hash_idempotency_key(str(uuid4()))
            )
    finally:
        repo.cancel(job_id, hash_idempotency_key(str(uuid4())))


def test_cleanup_commits_retry_cutoff_without_core_poll() -> None:
    assert DATABASE_URL is not None
    repo = _repo()
    request = _request()
    accepted, _ = _create(repo, request, str(uuid4()))
    job_id = str(accepted["jobId"])
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE trading_analytics_jobs "
            "SET retry_cutoff_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE job_id=%s",
            (job_id,),
        )
    repo.cleanup()
    status = repo.get(job_id)
    assert status is not None and status["status"] == "FAILED"
    assert status["failure"]["code"] == "RETRY_EXHAUSTED"
    _validate("TradingAnalyticsJobStatusResponse", status)


@pytest.mark.parametrize("ack_mode", ["PERSISTED", "DELETED_GENERATION", "STALE_ATTEMPT"])
def test_http_application_worker_and_shared_delivery_use_real_store(monkeypatch, ack_mode) -> None:
    """Use real providers/DB; only the two non-production artifact downloads are fixed."""
    import base64

    from starlette.testclient import TestClient

    from refinvest.app.config import Settings, get_settings
    from refinvest.app.main import app
    from refinvest.application.analytics_worker import (
        AnalyticsWorkerSettings,
        TradingAnalyticsWorkerHost,
    )
    from tests.test_trading_analytics_runtime import _vector

    assert DATABASE_URL is not None
    request = _request()
    settings = Settings(
        "ar03a-synthetic-key",
        database_url=DATABASE_URL,
        trading_review_active_job_limit=1000,
        trading_review_payload_encryption_key=base64.b64encode(b"A" * 32).decode(),
        trading_review_artifact_hosts=("core-artifacts.internal",),
        trading_analytics_implementation_digest=request["targetVersionSet"]["implementationDigest"],
    )
    vector = _vector()

    def fixed_download(*_):
        return (
            vector["artifactSamples"]["ledgerRevisionContent"],
            vector["artifactSamples"]["reconciliationManifest"],
        )

    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            base = "/trading-review/analytics-jobs"
            headers = {"X-Internal-Api-Key": "ar03a-synthetic-key", "Idempotency-Key": str(uuid4())}
            accepted = client.post(base, json=request, headers=headers)
            assert accepted.status_code == 202
            _validate("TradingAnalyticsJobAccepted", accepted.json())
            job_id = accepted.json()["jobId"]
            replay = client.post(base, json=request, headers=headers)
            assert replay.status_code == 200 and replay.json()["jobId"] == job_id
            status = client.get(f"{base}/{job_id}", headers=headers).json()
            update = {
                "mode": "REPLACE_QUEUED_GRANTS",
                "expectedGrantRevision": status["grantRevision"],
                "expectedAttemptToken": status["attemptToken"],
                "nextAttemptToken": status["attemptToken"],
                "expectedInputHash": request["expectedInputHash"],
                "versionSetHash": request["targetVersionSet"]["versionSetHash"],
                "deletionGeneration": request["deletionGeneration"],
                "expectedRetryCutoffAt": status["retryCutoffAt"],
                "ledgerRevisionContent": request["ledgerRevisionContent"],
                "reconciliationManifest": request["reconciliationManifest"],
            }
            receipt = client.post(
                f"{base}/{job_id}/input-grants",
                json=update,
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            assert receipt.status_code == 200 and receipt.json()["grantRevision"] == 1
            assert TradingAnalyticsWorkerHost(
                _repo(),
                AnalyticsWorkerSettings(
                    settings.trading_review_artifact_hosts,
                    settings.trading_analytics_implementation_digest,
                    settings.trading_analytics_max_input_bytes,
                ),
                worker_id="ar05a-worker",
                download_inputs=fixed_download,
            ).run_once()
            descriptor = client.get(f"{base}/{job_id}/result", headers=headers)
            assert descriptor.status_code == 200
            descriptor = descriptor.json()
            _validate("AnalyticsTerminalResultDescriptor", descriptor)
            resource = client.get(
                f"/trading-review/terminal-payloads/{descriptor['payloadId']}", headers=headers
            )
            assert resource.status_code == 200
            assert len(resource.content) == descriptor["byteSize"]
            assert hashlib.sha256(resource.content).hexdigest() == descriptor["payloadSha256"]
            _validate("AnalyticsTerminalPayload", resource.json())
            completed_replay = client.post(base, json=request, headers=headers)
            assert (
                completed_replay.status_code == 200
                and completed_replay.json()["status"] == "COMPLETED"
            )
            ack = {
                "coreAnalysisRunId": request["coreAnalysisRunId"],
                "payloadId": descriptor["payloadId"],
                "payloadSha256": descriptor["payloadSha256"],
                "terminalResultHash": descriptor["resultHash"],
                "attemptToken": request["attemptToken"],
                "deletionGeneration": request["deletionGeneration"],
                "disposition": "PERSISTED",
                "persistedAt": datetime.now(UTC).isoformat(),
            }
            if ack_mode != "PERSISTED":
                ack.pop("persistedAt")
                ack.update(
                    disposition="DISCARDED_STALE",
                    discardedAt=datetime.now(UTC).isoformat(),
                    staleReason=ack_mode,
                )
                if ack_mode == "DELETED_GENERATION":
                    ack["currentDeletionGeneration"] = request["deletionGeneration"] + 1
                else:
                    ack["currentAttemptToken"] = str(uuid4())
            ack_url = f"/trading-review/jobs/{job_id}/acknowledgement"
            assert client.post(ack_url, json=ack, headers=headers).status_code == 204
            assert client.post(ack_url, json=ack, headers=headers).status_code == 204
            conflicting_ack = dict(ack, terminalResultHash="0" * 64)
            conflict = client.post(ack_url, json=conflicting_ack, headers=headers)
            assert conflict.status_code == 409 and conflict.json()["code"] == "RESULT_HASH_MISMATCH"
            assert (
                client.get(
                    f"/trading-review/terminal-payloads/{descriptor['payloadId']}", headers=headers
                ).status_code
                == 404
            )
            other = _request()
            second = client.post(
                base, json=other, headers={**headers, "Idempotency-Key": str(uuid4())}
            )
            assert second.status_code == 202
            cancelled = client.post(
                f"{base}/{second.json()['jobId']}/cancellation",
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            assert cancelled.status_code == 202 and cancelled.json()["status"] == "CANCELLED"
    finally:
        app.dependency_overrides.clear()
