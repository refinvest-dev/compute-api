"""Durable Analytics V1 jobs with database-clock leases and fenced terminal writes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from os import urandom
from typing import Any, cast
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from refinvest.contracts.analytics.identity import jcs_sha256


class AnalyticsJobConflict(ValueError):
    pass


class AnalyticsJobCapacity(ValueError):
    pass


class AnalyticsJobUnavailable(ValueError):
    pass


def _db_time(cursor: Any) -> datetime:
    cursor.execute("SELECT CURRENT_TIMESTAMP AS now")
    return cast(datetime, cursor.fetchone()["now"])


def _stable_request(request: dict[str, object]) -> dict[str, object]:
    stable = dict(request)
    for role in ("ledgerRevisionContent", "reconciliationManifest"):
        reference = cast(dict[str, object], stable[role])
        stable[role] = {
            key: value for key, value in reference.items() if key not in {"accessUri", "expiresAt"}
        }
    return stable


def _grants(request: dict[str, object]) -> dict[str, object]:
    return {
        role: {
            "accessUri": cast(dict[str, object], request[role])["accessUri"],
            "expiresAt": cast(dict[str, object], request[role])["expiresAt"],
        }
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    }


def _expiry(grants: dict[str, object], now: datetime) -> datetime:
    expiries = [
        datetime.fromisoformat(
            str(cast(dict[str, object], grants[role])["expiresAt"]).replace("Z", "+00:00")
        )
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    ]
    if any(value <= now or value > now + timedelta(minutes=5) for value in expiries):
        raise AnalyticsJobConflict("INPUT_REFERENCE_EXPIRED")
    return min(expiries)


class PostgresAnalyticsJobRepository:
    def __init__(
        self,
        database_url: str,
        encryption_key: bytes,
        *,
        active_job_limit: int,
        max_payload_bytes: int,
    ) -> None:
        if len(encryption_key) != 32 or active_job_limit <= 0 or max_payload_bytes <= 0:
            raise ValueError("Analytics job store configuration is invalid.")
        self.database_url = database_url
        self.cipher = AESGCM(encryption_key)
        self.active_job_limit = active_job_limit
        self.max_payload_bytes = max_payload_bytes

    def _encrypt(self, identity: str, data: bytes) -> bytes:
        nonce = urandom(12)
        return nonce + self.cipher.encrypt(nonce, data, identity.encode())

    def _decrypt(self, identity: str, data: bytes) -> bytes:
        return self.cipher.decrypt(data[:12], data[12:], identity.encode())

    def _connection(self) -> Any:
        return connect(self.database_url, row_factory=cast(Any, dict_row))

    def create(
        self, request: dict[str, object], key_hash: str, fingerprint: str
    ) -> tuple[dict[str, object], bool]:
        job_id = str(uuid4())
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key_hash,))
            cursor.execute(
                "SELECT request_fingerprint, job_id FROM trading_analytics_idempotency "
                "WHERE idempotency_key_hash=%s",
                (key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise AnalyticsJobConflict("IDEMPOTENCY_CONFLICT")
                job_id = str(existing["job_id"])
                self._expire_retry(cursor, job_id)
                cursor.execute("SELECT * FROM trading_analytics_jobs WHERE job_id=%s", (job_id,))
                return self._status_row(cast(dict[str, Any], cursor.fetchone())), False
            cursor.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('trading-review-active-capacity', 0))"
            )
            cursor.execute(
                "SELECT 1 FROM trading_analytics_jobs WHERE core_analysis_run_id=%s "
                "OR (core_logical_job_id=%s AND status IN ('PENDING','RUNNING')) LIMIT 1",
                (request["coreAnalysisRunId"], request["coreLogicalJobId"]),
            )
            if cursor.fetchone() is not None:
                raise AnalyticsJobConflict("IDEMPOTENCY_CONFLICT")
            cursor.execute(
                "SELECT (SELECT COUNT(*) FROM trading_review_jobs "
                "WHERE status IN ('PENDING','RUNNING'))"
                " + (SELECT COUNT(*) FROM trading_analytics_jobs "
                "WHERE status IN ('PENDING','RUNNING')) AS active"
            )
            if cursor.fetchone()["active"] >= self.active_job_limit:
                raise AnalyticsJobCapacity("TRADING_REVIEW_CAPACITY_EXCEEDED")
            now = _db_time(cursor)
            grants = _grants(request)
            expire_at = _expiry(grants, now)
            cipher = self._encrypt(job_id, json.dumps(grants, separators=(",", ":")).encode())
            cursor.execute(
                """
                INSERT INTO trading_analytics_jobs (
                    job_id,core_logical_job_id,core_analysis_run_id,ledger_revision_id,status,
                    request_payload,request_fingerprint,expected_input_hash,analysis_config_hash,
                    version_set_hash,attempt_token,deletion_generation,
                    grants_encrypted,grants_expire_at,
                    retry_cutoff_at
                ) VALUES (%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    job_id,
                    request["coreLogicalJobId"],
                    request["coreAnalysisRunId"],
                    request["ledgerRevisionId"],
                    Jsonb(_stable_request(request)),
                    fingerprint,
                    request["expectedInputHash"],
                    request["analysisConfigHash"],
                    cast(dict[str, object], request["targetVersionSet"])["versionSetHash"],
                    request["attemptToken"],
                    request["deletionGeneration"],
                    cipher,
                    expire_at,
                    now + timedelta(hours=24),
                ),
            )
            cursor.execute(
                "INSERT INTO trading_analytics_attempts("
                "job_id,attempt_number,attempt_token,deletion_generation) "
                "VALUES (%s,1,%s,%s)",
                (job_id, request["attemptToken"], request["deletionGeneration"]),
            )
            cursor.execute(
                "INSERT INTO trading_analytics_idempotency("
                "idempotency_key_hash,request_fingerprint,job_id) VALUES (%s,%s,%s)",
                (key_hash, fingerprint, job_id),
            )
            return self._accepted(cursor, job_id), True

    @staticmethod
    def _accepted(cursor: Any, job_id: str) -> dict[str, object]:
        cursor.execute(
            "SELECT job_id,core_logical_job_id,core_analysis_run_id,attempt_token,"
            "deletion_generation,grant_revision,expected_input_hash,created_at "
            "FROM trading_analytics_jobs WHERE job_id=%s",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AnalyticsJobUnavailable("JOB_NOT_FOUND")
        return {
            "jobId": str(row["job_id"]),
            "coreLogicalJobId": row["core_logical_job_id"],
            "kind": "ANALYTICS",
            "status": "PENDING",
            "createdAt": row["created_at"].isoformat(),
            "coreAnalysisRunId": row["core_analysis_run_id"],
            "attempt": 1,
            "attemptToken": row["attempt_token"],
            "deletionGeneration": row["deletion_generation"],
            "grantRevision": row["grant_revision"],
            "expectedInputHash": row["expected_input_hash"],
        }

    @staticmethod
    def _expire_retry(cursor: Any, job_id: str) -> None:
        cursor.execute(
            """
            UPDATE trading_analytics_jobs SET status='FAILED',progress_stage='FAILED',
                failure=jsonb_build_object('code','RETRY_EXHAUSTED',
                    'message','Analytics retry budget expired.',
                    'retryable',false,'details',jsonb_build_array()),
                retry_available=false,grants_encrypted=NULL,grants_expire_at=NULL,
                terminal_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
            WHERE job_id=%s AND status='PENDING' AND retry_cutoff_at<=CURRENT_TIMESTAMP
            """,
            (job_id,),
        )

    def get(self, job_id: str) -> dict[str, object] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            self._expire_retry(cursor, job_id)
            cursor.execute("SELECT * FROM trading_analytics_jobs WHERE job_id=%s", (job_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._status_row(row)

    @staticmethod
    def _status_row(row: dict[str, Any]) -> dict[str, object]:
        request = cast(dict[str, object], row["request_payload"])
        status: dict[str, object] = {
            "jobId": str(row["job_id"]),
            "coreLogicalJobId": row["core_logical_job_id"],
            "coreAnalysisRunId": row["core_analysis_run_id"],
            "kind": "ANALYTICS",
            "status": row["status"],
            "progressStage": row["progress_stage"],
            "attempt": row["attempt_number"],
            "attemptToken": row["attempt_token"],
            "deletionGeneration": row["deletion_generation"],
            "grantRevision": row["grant_revision"],
            "expectedInputHash": row["expected_input_hash"],
            "inputHashSpecificationVersion": request["inputHashSpecificationVersion"],
            "analysisConfigHash": row["analysis_config_hash"],
            "targetVersionSet": request["targetVersionSet"],
            "retryPolicyVersion": request["retryPolicyVersion"],
            "resultDeliveryPolicy": request["resultDeliveryPolicy"],
            "retryCutoffAt": row["retry_cutoff_at"].isoformat(),
            "retryAvailable": row["retry_available"],
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
            "startedAt": row["started_at"].isoformat() if row["started_at"] else None,
        }
        if row["last_attempt_failure"] is not None:
            status["lastAttemptFailure"] = row["last_attempt_failure"]
        if row["status"] == "COMPLETED":
            status["terminalPayloadId"] = str(row["terminal_payload_id"])
            status["resultHash"] = row["result_hash"]
        if row["status"] == "FAILED":
            status["failure"] = row["failure"]
        if row["terminal_at"] is not None:
            status["terminalAt"] = row["terminal_at"].isoformat()
        return status

    def update_grants(
        self, job_id: str, update: dict[str, object], key_hash: str
    ) -> dict[str, object]:
        fingerprint = jcs_sha256(update)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key_hash,))
            cursor.execute(
                "SELECT request_fingerprint, receipt, job_id "
                "FROM trading_analytics_grant_updates WHERE idempotency_key_hash=%s",
                (key_hash,),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if replay["request_fingerprint"] != fingerprint or str(replay["job_id"]) != job_id:
                    raise AnalyticsJobConflict("IDEMPOTENCY_CONFLICT")
                return cast(dict[str, object], replay["receipt"])
            cursor.execute(
                "SELECT * FROM trading_analytics_jobs WHERE job_id=%s FOR UPDATE", (job_id,)
            )
            row = cursor.fetchone()
            if row is None:
                raise AnalyticsJobUnavailable("JOB_NOT_FOUND")
            cutoff = datetime.fromisoformat(
                str(update["expectedRetryCutoffAt"]).replace("Z", "+00:00")
            )
            if (
                row["status"] != "PENDING"
                or row["lease_owner"] is not None
                or row["grant_revision"] != update["expectedGrantRevision"]
                or row["attempt_token"] != update["expectedAttemptToken"]
                or row["expected_input_hash"] != update["expectedInputHash"]
                or row["version_set_hash"] != update["versionSetHash"]
                or row["deletion_generation"] != update["deletionGeneration"]
                or row["retry_cutoff_at"] != cutoff
            ):
                raise AnalyticsJobConflict("STALE_GRANT_UPDATE")
            stable = cast(dict[str, object], row["request_payload"])
            for role in ("ledgerRevisionContent", "reconciliationManifest"):
                expected = cast(dict[str, object], stable[role])
                actual = cast(dict[str, object], update[role])
                if any(expected[key] != actual.get(key) for key in expected):
                    raise AnalyticsJobConflict("ARTIFACT_IDENTITY_CONFLICT")
            now = _db_time(cursor)
            grants = _grants(update)
            expire_at = _expiry(grants, now)
            mode = str(update["mode"])
            next_token = str(update["nextAttemptToken"])
            if mode == "REPLACE_QUEUED_GRANTS":
                if (
                    row["retry_available"]
                    or row["last_attempt_failure"] is not None
                    or next_token != row["attempt_token"]
                ):
                    raise AnalyticsJobConflict("STALE_GRANT_UPDATE")
                attempt = row["attempt_number"]
            elif mode == "START_RETRY_ATTEMPT":
                if (
                    not row["retry_available"]
                    or row["last_attempt_failure"] is None
                    or next_token == row["attempt_token"]
                    or row["attempt_number"] >= 3
                    or now >= row["retry_cutoff_at"]
                ):
                    raise AnalyticsJobConflict("STALE_GRANT_UPDATE")
                cursor.execute(
                    "SELECT 1 FROM trading_analytics_attempts WHERE job_id=%s AND attempt_token=%s",
                    (job_id, next_token),
                )
                if cursor.fetchone() is not None:
                    raise AnalyticsJobConflict("STALE_GRANT_UPDATE")
                attempt = row["attempt_number"] + 1
                cursor.execute(
                    "INSERT INTO trading_analytics_attempts("
                    "job_id,attempt_number,attempt_token,deletion_generation) "
                    "VALUES (%s,%s,%s,%s)",
                    (job_id, attempt, next_token, row["deletion_generation"]),
                )
            else:
                raise AnalyticsJobConflict("STALE_GRANT_UPDATE")
            revision = row["grant_revision"] + 1
            encrypted = self._encrypt(job_id, json.dumps(grants, separators=(",", ":")).encode())
            cursor.execute(
                """
                UPDATE trading_analytics_jobs SET grants_encrypted=%s,grants_expire_at=%s,
                    grant_revision=%s,attempt_number=%s,attempt_token=%s,retry_available=false,
                    last_attempt_failure=NULL,progress_stage='QUEUED',updated_at=CURRENT_TIMESTAMP
                WHERE job_id=%s
                """,
                (encrypted, expire_at, revision, attempt, next_token, job_id),
            )
            receipt: dict[str, object] = {
                "jobId": job_id,
                "mode": mode,
                "grantRevision": revision,
                "attemptToken": next_token,
                "attempt": attempt,
                "status": "PENDING",
                "updatedAt": now.isoformat(),
            }
            cursor.execute(
                "INSERT INTO trading_analytics_grant_updates("
                "idempotency_key_hash,job_id,request_fingerprint,receipt) "
                "VALUES (%s,%s,%s,%s)",
                (key_hash, job_id, fingerprint, Jsonb(receipt)),
            )
            return receipt

    def cancel(self, job_id: str, key_hash: str) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key_hash,))
            cursor.execute(
                "SELECT job_id FROM trading_analytics_cancellations WHERE idempotency_key_hash=%s",
                (key_hash,),
            )
            replay = cursor.fetchone()
            if replay is not None and str(replay["job_id"]) != job_id:
                raise AnalyticsJobConflict("IDEMPOTENCY_CONFLICT")
            cursor.execute(
                "SELECT status,attempt_number FROM trading_analytics_jobs "
                "WHERE job_id=%s FOR UPDATE",
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise AnalyticsJobUnavailable("JOB_NOT_FOUND")
            if row["status"] in {"PENDING", "RUNNING"}:
                cursor.execute(
                    """
                    UPDATE trading_analytics_jobs SET status='CANCELLED',progress_stage='CANCELLED',
                        cancellation_requested=true,grants_encrypted=NULL,grants_expire_at=NULL,
                        lease_owner=NULL,lease_expires_at=NULL,retry_available=false,
                        terminal_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    WHERE job_id=%s
                    """,
                    (job_id,),
                )
                cursor.execute(
                    "UPDATE trading_analytics_attempts SET outcome='CANCELLED',"
                    "completed_at=CURRENT_TIMESTAMP WHERE job_id=%s "
                    "AND attempt_number=%s AND outcome IS NULL",
                    (job_id, row["attempt_number"]),
                )
            if replay is None:
                cursor.execute(
                    "INSERT INTO trading_analytics_cancellations("
                    "idempotency_key_hash,job_id) VALUES (%s,%s)",
                    (key_hash, job_id),
                )
            return row["status"] in {"PENDING", "RUNNING"}

    def claim_next(
        self, worker_id: str, lease: timedelta = timedelta(seconds=60)
    ) -> dict[str, object] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM trading_analytics_jobs
                WHERE (status='PENDING' AND retry_available=false
                    AND grants_expire_at>CURRENT_TIMESTAMP)
                   OR (status='RUNNING' AND lease_expires_at<CURRENT_TIMESTAMP)
                ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            generation = row["lease_generation"] + 1
            cursor.execute(
                """
                UPDATE trading_analytics_jobs SET status='RUNNING',progress_stage='FETCHING_INPUT',
                    lease_generation=%s,lease_owner=%s,
                    lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),
                    started_at=COALESCE(started_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
                WHERE job_id=%s
                """,
                (generation, worker_id, lease.total_seconds(), job_id),
            )
            cursor.execute(
                """
                UPDATE trading_analytics_attempts SET lease_generation=%s,lease_owner=%s,
                    lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second')
                WHERE job_id=%s AND attempt_number=%s AND outcome IS NULL
                """,
                (generation, worker_id, lease.total_seconds(), job_id, row["attempt_number"]),
            )
            grants = cast(
                dict[str, object], json.loads(self._decrypt(job_id, row["grants_encrypted"]))
            )
            return {
                "jobId": job_id,
                "request": row["request_payload"],
                "grants": grants,
                "attempt": row["attempt_number"],
                "attemptToken": row["attempt_token"],
                "deletionGeneration": row["deletion_generation"],
                "leaseGeneration": generation,
                "leaseOwner": worker_id,
                "grantRevision": row["grant_revision"],
                "createdAt": row["created_at"],
                "retryCutoffAt": row["retry_cutoff_at"],
            }

    @staticmethod
    def _claim_where() -> str:
        return (
            "job_id=%s AND status='RUNNING' AND attempt_number=%s AND attempt_token=%s "
            "AND deletion_generation=%s AND lease_generation=%s AND lease_owner=%s "
            "AND lease_expires_at>CURRENT_TIMESTAMP AND cancellation_requested=false"
        )

    @staticmethod
    def _claim_values(claim: dict[str, object]) -> tuple[object, ...]:
        return (
            claim["jobId"],
            claim["attempt"],
            claim["attemptToken"],
            claim["deletionGeneration"],
            claim["leaseGeneration"],
            claim["leaseOwner"],
        )

    def heartbeat(self, claim: dict[str, object], lease: timedelta = timedelta(seconds=60)) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trading_analytics_jobs SET "
                "lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),"
                "updated_at=CURRENT_TIMESTAMP WHERE " + self._claim_where(),
                (lease.total_seconds(), *self._claim_values(claim)),
            )
            if cursor.rowcount != 1:
                return False
            cursor.execute(
                "UPDATE trading_analytics_attempts SET "
                "lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second') "
                "WHERE job_id=%s AND attempt_number=%s AND lease_generation=%s AND outcome IS NULL",
                (lease.total_seconds(), claim["jobId"], claim["attempt"], claim["leaseGeneration"]),
            )
            return cursor.rowcount == 1

    def fail_attempt(self, claim: dict[str, object], code: str, *, retryable: bool) -> bool:
        safe_failure: dict[str, object] = {
            "code": code,
            "message": "Analytics job failed safely.",
            "retryable": retryable,
            "details": [],
        }
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempt_number,retry_cutoff_at FROM trading_analytics_jobs WHERE "
                + self._claim_where()
                + " FOR UPDATE",
                self._claim_values(claim),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            available = (
                retryable
                and row["attempt_number"] < 3
                and _db_time(cursor) < row["retry_cutoff_at"]
            )
            failure: dict[str, object] = (
                safe_failure
                if available or not retryable
                else {
                    "code": "RETRY_EXHAUSTED",
                    "message": "Analytics retry budget expired.",
                    "retryable": False,
                    "details": [],
                }
            )
            cursor.execute(
                """
                UPDATE trading_analytics_attempts SET failure=%s,outcome='FAILED',
                    completed_at=CURRENT_TIMESTAMP,
                    lease_owner=NULL,lease_expires_at=NULL
                WHERE job_id=%s AND attempt_number=%s AND lease_generation=%s AND outcome IS NULL
                """,
                (Jsonb(safe_failure), claim["jobId"], claim["attempt"], claim["leaseGeneration"]),
            )
            if cursor.rowcount != 1:
                return False
            cursor.execute(
                """
                UPDATE trading_analytics_jobs SET status=%s,progress_stage=%s,
                    last_attempt_failure=%s,retry_available=%s,failure=%s,
                    terminal_at=CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP END,
                    lease_owner=NULL,lease_expires_at=NULL,grants_encrypted=NULL,
                    grants_expire_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE job_id=%s
                """,
                (
                    "PENDING" if available else "FAILED",
                    "RETRY_REQUIRED" if available else "FAILED",
                    Jsonb(safe_failure),
                    available,
                    None if available else Jsonb(failure),
                    available,
                    claim["jobId"],
                ),
            )
            return True

    def publish_completed(
        self,
        claim: dict[str, object],
        payload: dict[str, object],
        body: bytes,
    ) -> bool:
        request = cast(dict[str, object], claim["request"])
        policy = cast(dict[str, object], request["resultDeliveryPolicy"])
        effective_cap = min(cast(int, policy["maxPayloadBytes"]), self.max_payload_bytes)
        if len(body) > effective_cap:
            raise AnalyticsJobConflict("RESULT_PAYLOAD_TOO_LARGE")
        digest = hashlib.sha256(body).hexdigest()
        payload_id = str(uuid4())
        created_at = datetime.fromisoformat(str(payload["createdAt"]).replace("Z", "+00:00"))
        terminal_at = datetime.fromisoformat(str(payload["terminalAt"]).replace("Z", "+00:00"))
        expires_at = terminal_at + timedelta(hours=24)
        descriptor: dict[str, object] = {
            "jobId": claim["jobId"],
            "coreLogicalJobId": request["coreLogicalJobId"],
            "coreAnalysisRunId": request["coreAnalysisRunId"],
            "payloadId": payload_id,
            "transportMode": "SINGLE_RESOURCE",
            "resultDeliveryPolicyVersion": "ANALYTICS_RESULT_DELIVERY_V1",
            "mediaType": "application/vnd.refinvest.trading-analytics+json",
            "contentEncoding": "identity",
            "byteSize": len(body),
            "payloadSha256": digest,
            "attemptToken": claim["attemptToken"],
            "deletionGeneration": claim["deletionGeneration"],
            "inputHash": payload["inputHash"],
            "inputHashSpecificationVersion": payload["inputHashSpecificationVersion"],
            "analysisConfigHash": payload["analysisConfigHash"],
            "targetVersionSet": payload["targetVersionSet"],
            "resultSchemaVersion": payload["resultSchemaVersion"],
            "resultHashSpecificationVersion": payload["resultHashSpecificationVersion"],
            "counts": payload["counts"],
            "sectionDigests": payload["sectionDigests"],
            "evidenceHash": payload["evidenceHash"],
            "resultHash": payload["resultHash"],
            "createdAt": created_at.isoformat(),
            "expiresAt": expires_at.isoformat(),
        }
        encrypted = self._encrypt(payload_id, body)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM trading_analytics_jobs WHERE " + self._claim_where() + " FOR UPDATE",
                self._claim_values(claim),
            )
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                """
                INSERT INTO trading_analytics_payloads (
                    payload_id,job_id,attempt_number,attempt_token,deletion_generation,
                    encrypted_bytes,byte_size,payload_sha256,descriptor,created_at,expires_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    payload_id,
                    claim["jobId"],
                    claim["attempt"],
                    claim["attemptToken"],
                    claim["deletionGeneration"],
                    encrypted,
                    len(body),
                    digest,
                    Jsonb(descriptor),
                    created_at,
                    expires_at,
                ),
            )
            cursor.execute(
                """
                UPDATE trading_analytics_jobs SET status='COMPLETED',progress_stage='COMPLETED',
                    terminal_payload_id=%s,result_hash=%s,terminal_at=%s,
                    grants_encrypted=NULL,grants_expire_at=NULL,
                    lease_owner=NULL,lease_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE job_id=%s
                """,
                (payload_id, payload["resultHash"], terminal_at, claim["jobId"]),
            )
            cursor.execute(
                "UPDATE trading_analytics_attempts SET outcome='COMPLETED',"
                "completed_at=CURRENT_TIMESTAMP,lease_owner=NULL,lease_expires_at=NULL "
                "WHERE job_id=%s AND attempt_number=%s AND lease_generation=%s "
                "AND outcome IS NULL",
                (claim["jobId"], claim["attempt"], claim["leaseGeneration"]),
            )
            if cursor.rowcount != 1:
                raise AnalyticsJobConflict("LEASE_LOST")
            return True

    def result_descriptor(self, job_id: str) -> dict[str, object] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.descriptor,p.expires_at,p.encrypted_bytes FROM trading_analytics_jobs j
                JOIN trading_analytics_payloads p ON p.payload_id=j.terminal_payload_id
                WHERE j.job_id=%s AND j.status='COMPLETED'
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if row["expires_at"] <= _db_time(cursor):
                raise AnalyticsJobUnavailable("RESULT_PAYLOAD_EXPIRED")
            return cast(dict[str, object], row["descriptor"]) if row["encrypted_bytes"] else None

    def payload_bytes(self, payload_id: str) -> bytes | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.encrypted_bytes,p.expires_at FROM trading_analytics_payloads p
                JOIN trading_analytics_jobs j ON j.job_id=p.job_id
                WHERE p.payload_id=%s AND j.status='COMPLETED'
                """,
                (payload_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if row["expires_at"] <= _db_time(cursor):
                raise AnalyticsJobUnavailable("RESULT_PAYLOAD_EXPIRED")
            if row["encrypted_bytes"] is None:
                return None
            return self._decrypt(payload_id, row["encrypted_bytes"])

    def acknowledge(self, job_id: str, body: dict[str, object]) -> None:
        fingerprint = jcs_sha256(body)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,core_analysis_run_id FROM trading_analytics_jobs "
                "WHERE job_id=%s FOR UPDATE",
                (job_id,),
            )
            job = cursor.fetchone()
            if job is None:
                raise AnalyticsJobUnavailable("JOB_NOT_FOUND")
            cursor.execute(
                "SELECT request_fingerprint FROM trading_analytics_ack_receipts WHERE job_id=%s",
                (job_id,),
            )
            receipt = cursor.fetchone()
            if receipt is not None:
                if receipt["request_fingerprint"] != fingerprint:
                    raise AnalyticsJobConflict("ACK_CONFLICT")
                return
            if (
                job["status"] != "COMPLETED"
                or body["coreAnalysisRunId"] != job["core_analysis_run_id"]
            ):
                raise AnalyticsJobConflict("ACK_CONFLICT")
            cursor.execute(
                "SELECT * FROM trading_analytics_payloads WHERE job_id=%s FOR UPDATE", (job_id,)
            )
            payload = cursor.fetchone()
            if payload is None or payload["expires_at"] <= _db_time(cursor):
                raise AnalyticsJobUnavailable("RESULT_PAYLOAD_EXPIRED")
            descriptor = cast(dict[str, object], payload["descriptor"])
            for field in ("payloadId", "payloadSha256", "attemptToken", "deletionGeneration"):
                if body[field] != descriptor[field]:
                    raise AnalyticsJobConflict("ACK_CONFLICT")
            if body["terminalResultHash"] != descriptor["resultHash"]:
                raise AnalyticsJobConflict("ACK_CONFLICT")
            if body["disposition"] == "DISCARDED_STALE":
                if body["staleReason"] == "DELETED_GENERATION":
                    if cast(int, body["currentDeletionGeneration"]) <= cast(
                        int, body["deletionGeneration"]
                    ):
                        raise AnalyticsJobConflict("ACK_CONFLICT")
                elif body["currentAttemptToken"] == body["attemptToken"]:
                    raise AnalyticsJobConflict("ACK_CONFLICT")
            cursor.execute(
                "INSERT INTO trading_analytics_ack_receipts("
                "job_id,request_fingerprint,disposition) VALUES (%s,%s,%s)",
                (job_id, fingerprint, body["disposition"]),
            )
            cursor.execute(
                "UPDATE trading_analytics_payloads SET encrypted_bytes=NULL,"
                "acknowledged_at=CURRENT_TIMESTAMP WHERE payload_id=%s",
                (body["payloadId"],),
            )

    def cleanup(self, batch_size: int = 1000) -> tuple[int, int]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE trading_analytics_jobs SET status='FAILED',progress_stage='FAILED',
                    failure=jsonb_build_object('code','RETRY_EXHAUSTED',
                        'message','Analytics retry budget expired.',
                        'retryable',false,'details',jsonb_build_array()),
                    retry_available=false,grants_encrypted=NULL,grants_expire_at=NULL,
                    terminal_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE job_id IN (
                    SELECT job_id FROM trading_analytics_jobs
                    WHERE status='PENDING' AND retry_cutoff_at<=CURRENT_TIMESTAMP
                    ORDER BY retry_cutoff_at LIMIT %s FOR UPDATE SKIP LOCKED
                )
                """,
                (batch_size,),
            )
            cursor.execute(
                """
                UPDATE trading_analytics_payloads SET encrypted_bytes=NULL
                WHERE payload_id IN (
                    SELECT payload_id FROM trading_analytics_payloads
                    WHERE encrypted_bytes IS NOT NULL AND expires_at<=CURRENT_TIMESTAMP
                    ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED
                )
                """,
                (batch_size,),
            )
            purged = cursor.rowcount
            cursor.execute(
                """
                DELETE FROM trading_analytics_jobs WHERE job_id IN (
                    SELECT job_id FROM trading_analytics_jobs
                    WHERE terminal_at IS NOT NULL
                      AND terminal_at + INTERVAL '24 hours'<=CURRENT_TIMESTAMP
                    ORDER BY terminal_at LIMIT %s FOR UPDATE SKIP LOCKED
                )
                """,
                (batch_size,),
            )
            return purged, cursor.rowcount
