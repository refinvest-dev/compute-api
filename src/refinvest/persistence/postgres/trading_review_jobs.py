"""PostgreSQL persistence for durable Trading Review normalization jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from os import urandom
from typing import cast
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import connect
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    SafeTradingReviewFailure,
    TerminalPayloadDescriptor,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
    TradingReviewJobSubmission,
)


class PostgresTradingReviewJobRepository:
    """Short transactions, database-clock leases, and immutable terminal payloads."""

    def __init__(self, database_url: str, payload_encryption_key: bytes | None = None) -> None:
        if payload_encryption_key is not None and len(payload_encryption_key) != 32:
            raise ValueError("Trading Review payload encryption key must be 32 bytes.")
        self._database_url = database_url
        self._payload_cipher = AESGCM(payload_encryption_key) if payload_encryption_key else None

    def create_idempotently(
        self, job: TradingReviewJob, idempotency_key_hash: str
    ) -> TradingReviewJobSubmission:
        try:
            return self._create_idempotently(job, idempotency_key_hash)
        except UniqueViolation as error:
            raise TradingReviewIdempotencyConflict(
                "An active normalization job already exists for the logical request."
            ) from error

    def _create_idempotently(
        self, job: TradingReviewJob, idempotency_key_hash: str
    ) -> TradingReviewJobSubmission:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_key(cursor, idempotency_key_hash)
            cursor.execute(
                """
                SELECT i.request_fingerprint, j.job_id, j.core_logical_job_id, j.kind,
                       j.created_at, j.request_payload
                FROM trading_review_job_idempotency AS i
                JOIN trading_review_jobs AS j ON j.job_id = i.job_id
                WHERE i.idempotency_key_hash = %s
                """,
                (idempotency_key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                fingerprint, job_id, logical_id, kind, created_at, prior_request = cast(
                    tuple[str, str, str, str, datetime, dict[str, object]], existing
                )
                if fingerprint != job.request_fingerprint:
                    raise TradingReviewIdempotencyConflict(
                        "Idempotency-Key was reused for a different request."
                    )
                if _grants_are_fresher(prior_request, job.request_payload):
                    cursor.execute(
                        """
                        UPDATE trading_review_jobs
                        SET request_payload = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE job_id = %s AND
                          (status = 'PENDING' OR
                           (status = 'RUNNING' AND lease_owner IS NULL))
                        """,
                        (Jsonb(job.request_payload), job_id),
                    )
                return TradingReviewJobSubmission(
                    str(job_id),
                    logical_id,
                    TradingReviewJobKind(kind),
                    created_at,
                    newly_created=False,
                )
            cursor.execute(
                """
                INSERT INTO trading_review_jobs
                    (job_id, core_logical_job_id, core_import_session_id, kind, status,
                     request_payload, request_fingerprint, expected_input_hash,
                     version_set_hash, deletion_generation, attempt_token)
                VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, %s)
                RETURNING created_at
                """,
                (
                    job.job_id,
                    job.core_logical_job_id,
                    job.core_import_session_id,
                    job.kind.value,
                    Jsonb(job.request_payload),
                    job.request_fingerprint,
                    job.expected_input_hash,
                    job.version_set_hash,
                    job.deletion_generation,
                    job.attempt_token,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Trading Review job was not persisted.")
            created_at = cast(tuple[datetime], row)[0]
            cursor.execute(
                """
                INSERT INTO trading_review_job_idempotency
                    (idempotency_key_hash, request_fingerprint, job_id)
                VALUES (%s, %s, %s)
                """,
                (idempotency_key_hash, job.request_fingerprint, job.job_id),
            )
        return TradingReviewJobSubmission(
            job.job_id, job.core_logical_job_id, job.kind, created_at, newly_created=True
        )

    def get(self, job_id: str) -> TradingReviewJobStatusView | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(_STATUS_QUERY + " WHERE j.job_id = %s", (job_id,))
            row = cursor.fetchone()
        return _status_view(row)

    def request_cancellation(
        self, job_id: str, idempotency_key_hash: str
    ) -> TradingReviewJobStatusView | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_key(cursor, idempotency_key_hash)
            cursor.execute(
                """SELECT job_id FROM trading_review_job_cancellations
                   WHERE idempotency_key_hash = %s""",
                (idempotency_key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None and str(existing[0]) != job_id:
                raise TradingReviewIdempotencyConflict(
                    "Cancellation Idempotency-Key was reused for another job."
                )
            cursor.execute(
                "SELECT status FROM trading_review_jobs WHERE job_id = %s FOR UPDATE", (job_id,)
            )
            job_row = cursor.fetchone()
            if job_row is None:
                return None
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO trading_review_job_cancellations (idempotency_key_hash, job_id)
                    VALUES (%s, %s)
                    """,
                    (idempotency_key_hash, job_id),
                )
            current = str(job_row[0])
            if current == TradingReviewJobStatus.PENDING.value:
                cursor.execute(
                    """
                    UPDATE trading_review_jobs
                    SET status = 'CANCELLED', progress_stage = 'CANCELLED',
                        cancellation_requested = TRUE, terminal_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = %s AND status = 'PENDING'
                    """,
                    (job_id,),
                )
            elif current == TradingReviewJobStatus.RUNNING.value:
                cursor.execute(
                    """
                    UPDATE trading_review_jobs
                    SET status = 'CANCELLED', progress_stage = 'CANCELLED',
                        cancellation_requested = TRUE, terminal_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP, claimed_worker = NULL,
                        lease_owner = NULL, lease_expires_at = NULL,
                        heartbeat_at = NULL, next_attempt_at = NULL
                    WHERE job_id = %s AND status = 'RUNNING'
                      AND (lease_owner IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    RETURNING attempt_count
                    """,
                    (job_id,),
                )
                cancelled = cursor.fetchone()
                if cancelled is not None:
                    cursor.execute(
                        """
                        UPDATE trading_review_job_attempts
                        SET completed_at = CURRENT_TIMESTAMP, outcome = 'CANCELLED'
                        WHERE job_id = %s AND attempt_number = %s
                          AND completed_at IS NULL
                        """,
                        (job_id, cancelled[0]),
                    )
                    _close_artifact_leases(cursor, job_id, int(cancelled[0]), "CANCELLED")
                else:
                    cursor.execute(
                        """
                        UPDATE trading_review_jobs
                        SET cancellation_requested = TRUE,
                            progress_stage = 'CANCELLATION_REQUESTED',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE job_id = %s AND status = 'RUNNING'
                        """,
                        (job_id,),
                    )
            cursor.execute(_STATUS_QUERY + " WHERE j.job_id = %s", (job_id,))
            row = cursor.fetchone()
        return _status_view(row)

    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> ClaimedTradingReviewJob | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        lease_owner = str(uuid4())
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET status = 'CANCELLED', progress_stage = 'CANCELLED',
                    terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, next_attempt_at = NULL
                WHERE status = 'RUNNING' AND cancellation_requested = TRUE
                  AND lease_expires_at <= CURRENT_TIMESTAMP
                RETURNING job_id, attempt_count
                """
            )
            for expired_job_id, attempt_number in cursor.fetchall():
                cursor.execute(
                    """
                    UPDATE trading_review_job_attempts
                    SET completed_at = CURRENT_TIMESTAMP, outcome = 'CANCELLED'
                    WHERE job_id = %s AND attempt_number = %s AND completed_at IS NULL
                    """,
                    (expired_job_id, attempt_number),
                )
                _close_artifact_leases(cursor, str(expired_job_id), attempt_number, "CANCELLED")
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET status = 'FAILED', progress_stage = 'EXECUTION',
                    failure = %s, terminal_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP, claimed_worker = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, next_attempt_at = NULL
                WHERE status = 'RUNNING' AND cancellation_requested = FALSE
                  AND attempt_count >= 3 AND lease_expires_at <= CURRENT_TIMESTAMP
                RETURNING job_id, attempt_count
                """,
                (
                    Jsonb(
                        _failure_json(
                            SafeTradingReviewFailure(
                                "TEMPORARY_DEPENDENCY_FAILURE",
                                "Normalization worker attempts were exhausted.",
                                False,
                                "EXECUTION",
                            )
                        )
                    ),
                ),
            )
            for expired_job_id, attempt_number in cursor.fetchall():
                cursor.execute(
                    """
                    UPDATE trading_review_job_attempts
                    SET completed_at = CURRENT_TIMESTAMP, outcome = 'EXPIRED'
                    WHERE job_id = %s AND attempt_number = %s AND completed_at IS NULL
                    """,
                    (expired_job_id, attempt_number),
                )
                _close_artifact_leases(cursor, str(expired_job_id), attempt_number, "EXPIRED")
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT job_id, status,
                           (status = 'RUNNING' AND lease_expires_at <= CURRENT_TIMESTAMP)
                               AS reclaimed_stale_lease
                    FROM trading_review_jobs
                    WHERE cancellation_requested = FALSE
                      AND attempt_count < 3
                      AND (
                        (status = 'PENDING' AND
                            (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP))
                        OR
                        (status = 'RUNNING' AND
                            (lease_expires_at <= CURRENT_TIMESTAMP OR
                             (lease_owner IS NULL AND next_attempt_at <= CURRENT_TIMESTAMP)))
                      )
                    ORDER BY created_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE trading_review_jobs AS j
                SET status = 'RUNNING', progress_stage = 'ARTIFACT_FETCH',
                    claimed_worker = %s, lease_owner = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    heartbeat_at = CURRENT_TIMESTAMP,
                    attempt_count = j.attempt_count + 1,
                    next_attempt_at = NULL,
                    started_at = COALESCE(j.started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP,
                    failure = NULL
                FROM candidate
                WHERE j.job_id = candidate.job_id
                RETURNING j.job_id, j.core_logical_job_id, j.core_import_session_id,
                    j.request_payload, j.expected_input_hash, j.version_set_hash,
                    j.deletion_generation, j.attempt_token, j.attempt_count, j.created_at,
                    candidate.reclaimed_stale_lease
                """,
                (worker_id, lease_owner, lease_duration.total_seconds()),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            values = cast(
                tuple[str, str, str, dict[str, object], str, str, int, str, int, datetime, bool],
                row,
            )
            if values[10]:
                cursor.execute(
                    """
                    UPDATE trading_review_job_attempts
                    SET completed_at = CURRENT_TIMESTAMP, outcome = 'EXPIRED'
                    WHERE job_id = %s AND attempt_number = %s AND completed_at IS NULL
                    """,
                    (values[0], values[8] - 1),
                )
                _close_artifact_leases(cursor, str(values[0]), values[8] - 1, "EXPIRED")
            cursor.execute(
                """
                INSERT INTO trading_review_job_attempts
                    (job_id, attempt_number, attempt_token, deletion_generation, lease_owner,
                     lease_expires_at)
                VALUES (%s, %s, %s, %s, %s,
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'))
                """,
                (
                    values[0],
                    values[8],
                    values[7],
                    values[6],
                    lease_owner,
                    lease_duration.total_seconds(),
                ),
            )
            for grant in cast(list[dict[str, object]], values[3].get("artifacts", [])):
                expires_at = datetime.fromisoformat(str(grant["expiresAt"]).replace("Z", "+00:00"))
                cursor.execute(
                    """
                    INSERT INTO trading_review_artifact_leases
                        (lease_id, job_id, attempt_number, artifact_role, core_artifact_id,
                         raw_sha256, grant_expires_at, deletion_generation, status, closed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s > CURRENT_TIMESTAMP THEN 'ACTIVE' ELSE 'EXPIRED' END,
                            CASE WHEN %s > CURRENT_TIMESTAMP THEN NULL ELSE CURRENT_TIMESTAMP END)
                    """,
                    (
                        str(uuid4()),
                        values[0],
                        values[8],
                        str(grant["role"]),
                        str(grant["coreArtifactId"]),
                        str(grant["rawSha256"]),
                        expires_at,
                        values[6],
                        expires_at,
                        expires_at,
                    ),
                )
        return ClaimedTradingReviewJob(
            job_id=str(values[0]),
            core_logical_job_id=values[1],
            core_import_session_id=values[2],
            request_payload=values[3],
            expected_input_hash=values[4],
            version_set_hash=values[5],
            deletion_generation=values[6],
            attempt_token=values[7],
            attempt_number=values[8],
            lease_owner=lease_owner,
            created_at=values[9],
            reclaimed_stale_lease=values[10],
        )

    def heartbeat(
        self,
        job_id: str,
        lease_owner: str,
        attempt_token: str,
        deletion_generation: int,
        lease_duration: timedelta,
    ) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET heartbeat_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_token = %s AND deletion_generation = %s
                  AND cancellation_requested = FALSE
                  AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING attempt_count
                """,
                (
                    lease_duration.total_seconds(),
                    job_id,
                    lease_owner,
                    attempt_token,
                    deletion_generation,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            attempt_number = int(row[0])
            cursor.execute(
                """
                UPDATE trading_review_job_attempts
                SET heartbeat_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
                WHERE job_id = %s AND attempt_number = %s AND lease_owner = %s
                  AND completed_at IS NULL
                """,
                (lease_duration.total_seconds(), job_id, attempt_number, lease_owner),
            )
            return cursor.rowcount == 1

    def cancellation_requested(
        self,
        job_id: str,
        lease_owner: str,
        attempt_token: str,
        deletion_generation: int,
    ) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cancellation_requested
                FROM trading_review_jobs
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (job_id, lease_owner, attempt_token, deletion_generation),
            )
            row = cursor.fetchone()
        return row is None or bool(row[0])

    def publish_completed(
        self,
        claim: ClaimedTradingReviewJob,
        *,
        payload: bytes,
        media_type: str,
        payload_sha256: str,
        input_hash: str,
        result_hash: str,
        retention: timedelta,
    ) -> bool:
        if retention <= timedelta(0):
            raise ValueError("retention must be positive.")
        if hashlib.sha256(payload).hexdigest() != payload_sha256:
            raise ValueError("payload_sha256 does not match payload bytes.")
        if self._payload_cipher is None:
            raise RuntimeError("Terminal payload encryption is not configured.")
        payload_id = str(uuid4())
        nonce = urandom(12)
        encrypted_payload = nonce + self._payload_cipher.encrypt(
            nonce, payload, payload_id.encode()
        )
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cancellation_requested
                FROM trading_review_jobs
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                FOR UPDATE
                """,
                _claim_identity(claim),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            if bool(row[0]):
                self._cancel_locked(cursor, claim)
                return False
            cursor.execute("SET CONSTRAINTS trading_review_jobs_terminal_payload_fk DEFERRED")
            cursor.execute(
                """
                INSERT INTO trading_review_terminal_payloads
                    (payload_id, job_id, attempt_number, attempt_token, deletion_generation,
                     media_type, payload_bytes, byte_size, payload_sha256, input_hash,
                     version_set_hash, result_hash, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'))
                """,
                (
                    payload_id,
                    claim.job_id,
                    claim.attempt_number,
                    claim.attempt_token,
                    claim.deletion_generation,
                    media_type,
                    encrypted_payload,
                    len(payload),
                    payload_sha256,
                    input_hash,
                    claim.version_set_hash,
                    result_hash,
                    retention.total_seconds(),
                ),
            )
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET status = 'COMPLETED', progress_stage = 'COMPLETED',
                    terminal_payload_id = %s, result_hash = %s, failure = NULL,
                    terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, next_attempt_at = NULL
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (payload_id, result_hash, *_claim_identity(claim)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Locked Trading Review job lost its lease unexpectedly.")
            self._finish_attempt(cursor, claim, "COMPLETED")
        return True

    def mark_failed(
        self, claim: ClaimedTradingReviewJob, failure: SafeTradingReviewFailure
    ) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cancellation_requested
                FROM trading_review_jobs
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                FOR UPDATE
                """,
                _claim_identity(claim),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            if bool(row[0]):
                self._cancel_locked(cursor, claim)
                return False
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET status = 'FAILED', progress_stage = %s, failure = %s,
                    terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, next_attempt_at = NULL
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (
                    failure.stage,
                    Jsonb(_failure_json(failure)),
                    *_claim_identity(claim),
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._finish_attempt(cursor, claim, "FAILED")
        return True

    def mark_cancelled(self, claim: ClaimedTradingReviewJob) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM trading_review_jobs
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                FOR UPDATE
                """,
                _claim_identity(claim),
            )
            if cursor.fetchone() is None:
                return False
            return self._cancel_locked(cursor, claim)

    def schedule_retry(
        self,
        claim: ClaimedTradingReviewJob,
        delay: timedelta,
        failure: SafeTradingReviewFailure,
    ) -> bool:
        if delay <= timedelta(0):
            raise ValueError("retry delay must be positive.")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cancellation_requested
                FROM trading_review_jobs
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                FOR UPDATE
                """,
                _claim_identity(claim),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            if bool(row[0]):
                self._cancel_locked(cursor, claim)
                return False
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET progress_stage = 'RETRY_WAIT', failure = NULL,
                    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    next_attempt_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (delay.total_seconds(), *_claim_identity(claim)),
            )
            if cursor.rowcount != 1:
                return False
            self._finish_attempt(cursor, claim, "RETRY")
        return True

    def get_result_descriptor(self, job_id: str) -> TerminalPayloadDescriptor | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.job_id, j.core_logical_job_id, p.payload_id, p.media_type,
                       p.byte_size, p.payload_sha256, p.input_hash, p.version_set_hash,
                       p.result_hash, p.created_at, p.expires_at
                FROM trading_review_terminal_payloads AS p
                JOIN trading_review_jobs AS j ON j.job_id = p.job_id
                WHERE p.job_id = %s AND j.status = 'COMPLETED'
                  AND p.expires_at > CURRENT_TIMESTAMP
                """,
                (job_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if self._payload_cipher is None:
            raise RuntimeError("Terminal payload encryption is not configured.")
        values = cast(tuple[str, str, str, str, int, str, str, str, str, datetime, datetime], row)
        return TerminalPayloadDescriptor(
            str(values[0]),
            values[1],
            str(values[2]),
            values[3],
            values[4],
            values[5],
            values[6],
            values[7],
            values[8],
            values[9],
            values[10],
        )

    def get_terminal_payload(self, payload_id: str) -> bytes | None:
        cipher = self._payload_cipher
        if cipher is None:
            raise RuntimeError("Terminal payload encryption is not configured.")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload_bytes, payload_sha256, byte_size
                FROM trading_review_terminal_payloads
                WHERE payload_id = %s AND expires_at > CURRENT_TIMESTAMP
                """,
                (payload_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        encrypted = bytes(row[0])
        if len(encrypted) < 28:
            raise RuntimeError("Terminal payload integrity check failed.")
        try:
            payload = cipher.decrypt(encrypted[:12], encrypted[12:], payload_id.encode())
        except InvalidTag as error:
            raise RuntimeError("Terminal payload integrity check failed.") from error
        if len(payload) != row[2] or hashlib.sha256(payload).hexdigest() != row[1]:
            raise RuntimeError("Terminal payload integrity check failed.")
        return payload

    def acknowledge(
        self,
        job_id: str,
        *,
        idempotency_key_hash: str,
        core_aggregate_id: str,
        payload_id: str,
        payload_sha256: str,
        terminal_result_hash: str,
        persisted_at: datetime,
    ) -> bool:
        fingerprint = _ack_fingerprint(
            core_aggregate_id,
            payload_id,
            payload_sha256,
            terminal_result_hash,
            persisted_at,
        )
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_key(cursor, idempotency_key_hash)
            cursor.execute(
                """
                SELECT job_id, request_fingerprint
                FROM trading_review_job_acknowledgements
                WHERE idempotency_key_hash = %s
                """,
                (idempotency_key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[0]) != job_id or str(existing[1]) != fingerprint:
                    raise TradingReviewIdempotencyConflict(
                        "Acknowledgement Idempotency-Key was reused for another request."
                    )
                return True
            cursor.execute(
                """
                SELECT j.core_import_session_id, j.status, p.payload_sha256, p.result_hash
                FROM trading_review_jobs AS j
                JOIN trading_review_terminal_payloads AS p
                  ON p.payload_id = j.terminal_payload_id AND p.job_id = j.job_id
                WHERE j.job_id = %s AND p.payload_id = %s
                FOR UPDATE OF j, p
                """,
                (job_id, payload_id),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            aggregate_id, job_status, stored_payload_hash, stored_result_hash = cast(
                tuple[str, str, str, str], row
            )
            if (
                job_status != TradingReviewJobStatus.COMPLETED.value
                or aggregate_id != core_aggregate_id
                or stored_payload_hash != payload_sha256
                or stored_result_hash != terminal_result_hash
            ):
                raise TradingReviewJobStateConflict(
                    "Acknowledgement does not match the completed job payload."
                )
            cursor.execute(
                """
                UPDATE trading_review_terminal_payloads
                SET acknowledged_at = COALESCE(acknowledged_at, CURRENT_TIMESTAMP),
                    acknowledged_core_aggregate_id = COALESCE(
                        acknowledged_core_aggregate_id, %s
                    ),
                    persisted_at = COALESCE(persisted_at, %s)
                WHERE payload_id = %s AND job_id = %s
                """,
                (core_aggregate_id, persisted_at, payload_id, job_id),
            )
            cursor.execute(
                """
                INSERT INTO trading_review_job_acknowledgements
                    (idempotency_key_hash, job_id, payload_id, request_fingerprint)
                VALUES (%s, %s, %s, %s)
                """,
                (idempotency_key_hash, job_id, payload_id, fingerprint),
            )
        return True

    def delete_cleanup_candidates(self, batch_size: int) -> int:
        """Delete acknowledged or expired terminal jobs; active/leased jobs never qualify."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT j.job_id
                    FROM trading_review_jobs AS j
                    LEFT JOIN trading_review_terminal_payloads AS p
                      ON p.job_id = j.job_id
                    WHERE j.status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                      AND j.lease_owner IS NULL
                      AND (
                        j.terminal_at <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                        OR p.acknowledged_at IS NOT NULL
                        OR p.expires_at <= CURRENT_TIMESTAMP
                      )
                    ORDER BY j.terminal_at, j.job_id
                    LIMIT %s
                    FOR UPDATE OF j SKIP LOCKED
                )
                DELETE FROM trading_review_jobs AS j
                USING expired
                WHERE j.job_id = expired.job_id
                """,
                (batch_size,),
            )
            removed_jobs = cursor.rowcount
            cursor.execute(
                """
                DELETE FROM trading_review_job_acknowledgements
                WHERE idempotency_key_hash IN (
                    SELECT idempotency_key_hash
                    FROM trading_review_job_acknowledgements
                    WHERE expires_at <= CURRENT_TIMESTAMP
                    ORDER BY expires_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                """,
                (batch_size,),
            )
            return removed_jobs

    @staticmethod
    def _finish_attempt(cursor: object, claim: ClaimedTradingReviewJob, outcome: str) -> None:
        cursor.execute(  # type: ignore[attr-defined]
            """
            UPDATE trading_review_job_attempts
            SET completed_at = CURRENT_TIMESTAMP, outcome = %s
            WHERE job_id = %s AND attempt_number = %s AND lease_owner = %s
              AND attempt_token = %s AND deletion_generation = %s
              AND completed_at IS NULL
            """,
            (
                outcome,
                claim.job_id,
                claim.attempt_number,
                claim.lease_owner,
                claim.attempt_token,
                claim.deletion_generation,
            ),
        )
        lease_status = "CONSUMED" if outcome == "COMPLETED" else "CANCELLED"
        _close_artifact_leases(cursor, claim.job_id, claim.attempt_number, lease_status)

    @classmethod
    def _cancel_locked(cls, cursor: object, claim: ClaimedTradingReviewJob) -> bool:
        cursor.execute(  # type: ignore[attr-defined]
            """
            UPDATE trading_review_jobs
            SET status = 'CANCELLED', progress_stage = 'CANCELLED',
                terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                heartbeat_at = NULL, next_attempt_at = NULL, failure = NULL
            WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
              AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
              AND lease_expires_at > CURRENT_TIMESTAMP
            """,
            _claim_identity(claim),
        )
        if cursor.rowcount != 1:  # type: ignore[attr-defined]
            return False
        cls._finish_attempt(cursor, claim, "CANCELLED")
        return True


_STATUS_QUERY = """
SELECT j.job_id, j.core_logical_job_id, j.core_import_session_id, j.kind, j.status,
       j.progress_stage, j.attempt_count, j.attempt_token, j.deletion_generation,
       j.created_at, j.updated_at, j.started_at, j.terminal_at, j.failure,
       j.terminal_payload_id, j.result_hash, j.cancellation_requested
FROM trading_review_jobs AS j
"""


def _status_view(row: object | None) -> TradingReviewJobStatusView | None:
    if row is None:
        return None
    values = cast(
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            int,
            str,
            int,
            datetime,
            datetime,
            datetime | None,
            datetime | None,
            dict[str, object] | None,
            str | None,
            str | None,
            bool,
        ],
        row,
    )
    failure_data = values[13]
    failure = None
    if failure_data is not None:
        raw_details = failure_data.get("details", [])
        details = (
            tuple(cast(dict[str, object], item) for item in cast(list[object], raw_details))
            if isinstance(raw_details, list)
            else ()
        )
        failure = SafeTradingReviewFailure(
            code=str(failure_data["code"]),
            message=str(failure_data["message"]),
            retryable=bool(failure_data["retryable"]),
            stage=str(failure_data["stage"]),
            details=cast(tuple[dict[str, object], ...], details),
        )
    return TradingReviewJobStatusView(
        job_id=str(values[0]),
        core_logical_job_id=values[1],
        core_import_session_id=values[2],
        kind=TradingReviewJobKind(values[3]),
        status=TradingReviewJobStatus(values[4]),
        progress_stage=values[5],
        attempt=values[6],
        attempt_token=values[7],
        deletion_generation=values[8],
        created_at=values[9],
        updated_at=values[10],
        started_at=values[11],
        terminal_at=values[12],
        failure=failure,
        terminal_payload_id=str(values[14]) if values[14] is not None else None,
        result_hash=values[15],
        cancellation_requested=values[16],
    )


def _claim_identity(claim: ClaimedTradingReviewJob) -> tuple[object, ...]:
    return (
        claim.job_id,
        claim.lease_owner,
        claim.attempt_number,
        claim.attempt_token,
        claim.deletion_generation,
    )


def _failure_json(failure: SafeTradingReviewFailure) -> dict[str, object]:
    return {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
        "stage": failure.stage,
        "details": list(failure.details),
    }


def _lock_key(cursor: object, key_hash: str) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key_hash,)
    )


def _ack_fingerprint(
    core_aggregate_id: str,
    payload_id: str,
    payload_sha256: str,
    terminal_result_hash: str,
    persisted_at: datetime,
) -> str:
    payload = {
        "coreAggregateId": core_aggregate_id,
        "payloadId": payload_id,
        "payloadSha256": payload_sha256,
        "terminalResultHash": terminal_result_hash,
        "persistedAt": persisted_at.isoformat(),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _close_artifact_leases(cursor: object, job_id: str, attempt_number: int, status: str) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        """
        UPDATE trading_review_artifact_leases
        SET status = %s, closed_at = CURRENT_TIMESTAMP
        WHERE job_id = %s AND attempt_number = %s AND status = 'ACTIVE'
        """,
        (status, job_id, attempt_number),
    )


def _grants_are_fresher(old: dict[str, object], new: dict[str, object]) -> bool:
    def expiries(payload: dict[str, object]) -> dict[str, datetime]:
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            return {}
        values: dict[str, datetime] = {}
        for item in cast(list[object], artifacts):
            if not isinstance(item, dict):
                return {}
            grant = cast(dict[str, object], item)
            role, expires_at = grant.get("role"), grant.get("expiresAt")
            if not isinstance(role, str) or not isinstance(expires_at, str):
                return {}
            try:
                values[role] = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                return {}
        return values

    before, after = expiries(old), expiries(new)
    return (
        len(before) == 2
        and before.keys() == after.keys()
        and all(after[role] >= expiry for role, expiry in before.items())
        and any(after[role] > expiry for role, expiry in before.items())
    )
