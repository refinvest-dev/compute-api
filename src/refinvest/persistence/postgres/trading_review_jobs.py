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

from refinvest.app.trading_review_result_delivery import effective_payload_limit
from refinvest.app.trading_review_result_integrity import (
    ResultIntegrityError,
    parse_result_json_bytes,
    verify_public_result,
)
from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    GrantUpdateReceipt,
    SafeTradingReviewFailure,
    TerminalPayloadDescriptor,
    TradingReviewCapacityExceeded,
    TradingReviewGrantInvalid,
    TradingReviewIdempotencyConflict,
    TradingReviewJob,
    TradingReviewJobKind,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
    TradingReviewJobSubmission,
)


class ResultPayloadLimitExceeded(ValueError):
    """The exact entity body exceeds the pinned effective limit."""


class PostgresTradingReviewJobRepository:
    """Short transactions, database-clock leases, and immutable terminal payloads."""

    def __init__(
        self,
        database_url: str,
        payload_encryption_key: bytes | None = None,
        *,
        active_job_limit: int = 20,
        grant_renewal_cutoff: timedelta = timedelta(hours=1),
        grant_refresh_wait: timedelta = timedelta(minutes=5),
        max_attempts: int = 3,
        max_payload_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if payload_encryption_key is not None and len(payload_encryption_key) != 32:
            raise ValueError("Trading Review payload encryption key must be 32 bytes.")
        self._database_url = database_url
        self._payload_cipher = AESGCM(payload_encryption_key) if payload_encryption_key else None
        if active_job_limit <= 0 or max_attempts <= 0:
            raise ValueError("Job capacity and attempt budget must be positive.")
        if grant_renewal_cutoff <= timedelta(0) or grant_refresh_wait <= timedelta(0):
            raise ValueError("Grant renewal durations must be positive.")
        self._active_job_limit = active_job_limit
        self._grant_renewal_cutoff = grant_renewal_cutoff
        self._grant_refresh_wait = grant_refresh_wait
        self._max_attempts = max_attempts
        if max_payload_bytes <= 0:
            raise ValueError("Trading Review result payload limit must be positive.")
        self._max_payload_bytes = max_payload_bytes

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
                       j.created_at
                FROM trading_review_job_idempotency AS i
                JOIN trading_review_jobs AS j ON j.job_id = i.job_id
                WHERE i.idempotency_key_hash = %s
                """,
                (idempotency_key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                fingerprint, job_id, logical_id, kind, created_at = cast(
                    tuple[str, str, str, str, datetime], existing
                )
                if fingerprint != job.request_fingerprint:
                    raise TradingReviewIdempotencyConflict(
                        "Idempotency-Key was reused for a different request."
                    )
                return TradingReviewJobSubmission(
                    str(job_id),
                    logical_id,
                    TradingReviewJobKind(kind),
                    created_at,
                    newly_created=False,
                )
            _lock_key(cursor, "trading-review-active-capacity")
            cursor.execute(
                "SELECT (SELECT COUNT(*) FROM trading_review_jobs "
                "WHERE status IN ('PENDING', 'RUNNING')) + "
                "(SELECT COUNT(*) FROM trading_analytics_jobs "
                "WHERE status IN ('PENDING', 'RUNNING'))"
            )
            count_row = cursor.fetchone()
            if count_row is None:
                raise RuntimeError("Trading Review capacity query returned no row.")
            if int(count_row[0]) >= self._active_job_limit:
                raise TradingReviewCapacityExceeded("Trading Review active-job pool is full.")
            grants = cast(
                list[dict[str, object]], job.request_payload.get("artifactAccessGrants", [])
            )
            if len(grants) != 2:
                raise ValueError("Exactly two artifact grants are required.")
            if not _fresh_grants(grants, cursor):
                raise TradingReviewGrantInvalid("New admission requires fresh grants.")
            stored_payload = _without_grant_locators(job.request_payload)
            encrypted_grants = self._encrypt_grants(job.job_id, grants)
            cursor.execute(
                """
                INSERT INTO trading_review_jobs
                    (job_id, core_logical_job_id, core_import_session_id, kind, status,
                     request_payload, request_fingerprint, expected_input_set_hash,
                     version_set_hash, deletion_generation, attempt_token,
                     grant_renewal_cutoff_at, current_grants_encrypted,
                     core_normalization_run_id, input_identity, computed_input_set_hash,
                     hash_specification_version, retry_policy_version, target_version_set,
                     result_delivery_policy)
                VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, %s,
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), %s,
                        %s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at
                """,
                (
                    job.job_id,
                    job.core_logical_job_id,
                    job.core_import_session_id,
                    job.kind.value,
                    Jsonb(stored_payload),
                    job.request_fingerprint,
                    job.expected_input_set_hash,
                    job.version_set_hash,
                    job.deletion_generation,
                    job.attempt_token,
                    self._grant_renewal_cutoff.total_seconds(),
                    encrypted_grants,
                    job.core_normalization_run_id,
                    Jsonb(job.request_payload["inputIdentity"]),
                    job.computed_input_set_hash,
                    job.hash_specification_version,
                    job.retry_policy_version,
                    Jsonb(
                        cast(
                            dict[str, object],
                            cast(dict[str, object], job.request_payload["inputIdentity"])[
                                "targetVersionSet"
                            ],
                        )
                    ),
                    Jsonb(job.request_payload["resultDeliveryPolicy"]),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Trading Review job was not persisted.")
            created_at = cast(tuple[datetime], row)[0]
            cursor.execute(
                "INSERT INTO trading_review_attempt_tokens (job_id, attempt_token) VALUES (%s, %s)",
                (job.job_id, job.attempt_token),
            )
            _insert_grant_revision(cursor, job.job_id, 0, job.attempt_token, grants)
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
            self._reconcile_job(cursor, job_id)
            cursor.execute(_STATUS_QUERY + " WHERE j.job_id = %s", (job_id,))
            row = cursor.fetchone()
        return _status_view(row)

    def _encrypt_grants(self, job_id: str, grants: list[dict[str, object]]) -> bytes:
        if self._payload_cipher is None:
            raise RuntimeError("Trading Review encryption is not configured.")
        nonce = urandom(12)
        raw = json.dumps(grants, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return nonce + self._payload_cipher.encrypt(nonce, raw, job_id.encode())

    def _with_grant_locators(
        self, job_id: str, payload: dict[str, object], encrypted: bytes | None
    ) -> dict[str, object]:
        if encrypted is None:
            return payload  # Live jobs created before migration 0011 retain their original grants.
        if self._payload_cipher is None:
            raise RuntimeError("Trading Review encryption is not configured.")
        raw = bytes(encrypted)
        grants = json.loads(self._payload_cipher.decrypt(raw[:12], raw[12:], job_id.encode()))
        return {**payload, "artifactAccessGrants": grants}

    def _reconcile_job(self, cursor: object, job_id: str) -> None:
        """Observe expiry with the DB clock even when all workers have stopped."""
        cursor.execute(  # type: ignore[attr-defined]
            """SELECT status, attempt_count, lease_owner, lease_expires_at,
                      grant_action, grant_refresh_deadline_at, grant_renewal_cutoff_at,
                      cancellation_requested, grant_revision, CURRENT_TIMESTAMP
               FROM trading_review_jobs WHERE job_id = %s FOR UPDATE""",
            (job_id,),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None or row[0] not in ("PENDING", "RUNNING"):
            return
        row = cast(
            tuple[
                str,
                int,
                str | None,
                datetime | None,
                str,
                datetime | None,
                datetime,
                bool,
                int,
                datetime,
            ],
            row,
        )
        (
            status,
            attempt,
            lease_owner,
            lease_expiry,
            action,
            deadline,
            cutoff,
            cancelled,
            revision,
            now,
        ) = row
        if (
            status == "RUNNING"
            and lease_owner is not None
            and lease_expiry is not None
            and lease_expiry <= now
        ):
            cursor.execute(  # type: ignore[attr-defined]
                """UPDATE trading_review_job_attempts SET completed_at = CURRENT_TIMESTAMP,
                          outcome = 'EXPIRED'
                   WHERE job_id = %s AND attempt_number = %s AND completed_at IS NULL""",
                (job_id, attempt),
            )
            _close_artifact_leases(cursor, job_id, attempt, "EXPIRED")
            cursor.execute(  # type: ignore[attr-defined]
                """UPDATE trading_review_jobs SET lease_owner = NULL, claimed_worker = NULL,
                          lease_expires_at = NULL, heartbeat_at = NULL,
                          updated_at = CURRENT_TIMESTAMP WHERE job_id = %s""",
                (job_id,),
            )
            lease_owner = None
            if not cancelled and attempt < self._max_attempts:
                action = "START_RETRY_ATTEMPT"
        if cancelled:
            cursor.execute(  # type: ignore[attr-defined]
                """UPDATE trading_review_jobs SET status = 'CANCELLED',
                          progress_stage = 'CANCELLED', grant_action = 'NONE',
                          grant_refresh_deadline_at = NULL, terminal_at = CURRENT_TIMESTAMP,
                          current_grants_encrypted = NULL,
                          updated_at = CURRENT_TIMESTAMP WHERE job_id = %s
                          AND lease_owner IS NULL""",
                (job_id,),
            )
            return
        if status == "PENDING" and action == "NONE":
            cursor.execute(  # type: ignore[attr-defined]
                """SELECT COUNT(*) FROM trading_review_grant_revisions
                   WHERE job_id = %s AND grant_revision = %s AND status = 'ACTIVE'
                     AND grant_expires_at > CURRENT_TIMESTAMP""",
                (job_id, revision),
            )
            if cursor.fetchone()[0] != 2:  # type: ignore[attr-defined]
                action = "REPLACE_QUEUED_GRANTS"
                cursor.execute(  # type: ignore[attr-defined]
                    """UPDATE trading_review_grant_revisions SET status = 'EXPIRED',
                              closed_at = CURRENT_TIMESTAMP
                       WHERE job_id = %s AND grant_revision = %s AND status = 'ACTIVE'""",
                    (job_id, revision),
                )
        if action != "NONE" and deadline is None:
            cursor.execute(  # type: ignore[attr-defined]
                """UPDATE trading_review_jobs
                   SET grant_action = %s, progress_stage = 'GRANT_REFRESH_WAIT',
                       grant_refresh_deadline_at = LEAST(
                           CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                           grant_renewal_cutoff_at), updated_at = CURRENT_TIMESTAMP
                   WHERE job_id = %s""",
                (action, self._grant_refresh_wait.total_seconds(), job_id),
            )
            deadline = min(now + self._grant_refresh_wait, cutoff)
        if (action != "NONE" and deadline is not None and now >= deadline) or (
            status == "RUNNING" and lease_owner is None and attempt >= self._max_attempts
        ):
            failure = SafeTradingReviewFailure(
                "GRANT_REFRESH_DEADLINE_EXCEEDED",
                "Normalization grant renewal window or attempt budget was exhausted.",
                False,
                "ARTIFACT_FETCH",
            )
            cursor.execute(  # type: ignore[attr-defined]
                """UPDATE trading_review_jobs SET status = 'FAILED',
                          progress_stage = 'ARTIFACT_FETCH',
                          failure = %s, terminal_at = CURRENT_TIMESTAMP,
                          grant_action = 'NONE', grant_refresh_deadline_at = NULL,
                          current_grants_encrypted = NULL,
                          updated_at = CURRENT_TIMESTAMP WHERE job_id = %s""",
                (Jsonb(_failure_json(failure)), job_id),
            )

    def update_grants(
        self, job_id: str, idempotency_key_hash: str, payload: dict[str, object]
    ) -> GrantUpdateReceipt | None:
        fingerprint = _canonical_hash(payload)
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_key(cursor, idempotency_key_hash)
            cursor.execute(
                """SELECT job_id, request_fingerprint, receipt
                   FROM trading_review_grant_update_receipts
                   WHERE idempotency_key_hash = %s""",
                (idempotency_key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[0]) != job_id or existing[1] != fingerprint:
                    raise TradingReviewIdempotencyConflict("Grant update key was reused.")
                return _grant_receipt(cast(dict[str, object], existing[2]))
            self._reconcile_job(cursor, job_id)
            cursor.execute(
                """SELECT status, grant_action, grant_revision, attempt_token,
                          expected_input_set_hash, version_set_hash, deletion_generation,
                          attempt_count, lease_owner, request_payload, current_grants_encrypted,
                          grant_refresh_deadline_at, CURRENT_TIMESTAMP
                   FROM trading_review_jobs WHERE job_id = %s FOR UPDATE""",
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            (
                status,
                action,
                revision,
                token,
                input_hash,
                version_hash,
                generation,
                attempt,
                lease_owner,
                request,
                encrypted,
                deadline,
                now,
            ) = cast(
                tuple[
                    str,
                    str,
                    int,
                    str,
                    str,
                    str,
                    int,
                    int,
                    str | None,
                    dict[str, object],
                    bytes | None,
                    datetime | None,
                    datetime,
                ],
                row,
            )
            mode = payload["mode"]
            next_token = payload["nextAttemptToken"]
            if (
                action != mode
                or status not in ("PENDING", "RUNNING")
                or lease_owner is not None
                or deadline is None
                or now >= deadline
                or int(str(payload["expectedGrantRevision"])) != revision
                or payload["expectedAttemptToken"] != token
                or payload["expectedInputSetHash"] != input_hash
                or payload["versionSetHash"] != version_hash
                or payload["deletionGeneration"] != generation
                or (
                    mode == "REPLACE_QUEUED_GRANTS"
                    and (status != "PENDING" or attempt != 0 or next_token != token)
                )
                or (
                    mode == "START_RETRY_ATTEMPT"
                    and (
                        status != "RUNNING"
                        or attempt == 0
                        or attempt >= self._max_attempts
                        or next_token == token
                    )
                )
            ):
                raise TradingReviewJobStateConflict("Grant update does not match current state.")
            grants = cast(list[dict[str, object]], payload["artifactAccessGrants"])
            pinned = cast(list[dict[str, object]], request["artifactAccessGrants"])
            fields = ("role", "coreArtifactId", "rawSha256", "byteSize", "contentType")
            if (
                len(grants) != 2
                or {str(g["role"]) for g in grants} != {"TRADE_HISTORY", "POSITION_HISTORY"}
                or sorted(tuple(g.get(k) for k in fields) for g in grants)
                != sorted(tuple(g.get(k) for k in fields) for g in pinned)
            ):
                raise TradingReviewJobStateConflict("Replacement grants are invalid.")
            if not _fresh_grants(grants, cursor):
                raise TradingReviewGrantInvalid("Replacement grants are expired.")
            prior = self._with_grant_locators(job_id, request, encrypted)
            old = cast(list[dict[str, object]], prior["artifactAccessGrants"])
            if any(
                next(g for g in grants if g["role"] == old_grant["role"]).get("accessUri")
                == old_grant.get("accessUri")
                for old_grant in old
            ):
                raise TradingReviewJobStateConflict("Consumed grant may not be reused.")
            if mode == "START_RETRY_ATTEMPT":
                cursor.execute(
                    """INSERT INTO trading_review_attempt_tokens (job_id, attempt_token)
                       VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                    (job_id, next_token),
                )
                if cursor.rowcount != 1:
                    raise TradingReviewJobStateConflict("Attempt token was already used.")
            cursor.execute(
                """UPDATE trading_review_grant_revisions SET status = 'REPLACED',
                          closed_at = CURRENT_TIMESTAMP
                   WHERE job_id = %s AND grant_revision = %s AND status = 'ACTIVE'""",
                (job_id, revision),
            )
            new_revision = int(revision) + 1
            _insert_grant_revision(cursor, job_id, new_revision, str(next_token), grants)
            cursor.execute(
                """UPDATE trading_review_jobs
                   SET grant_revision = %s, attempt_token = %s,
                       current_grants_encrypted = %s,
                       request_payload = %s, grant_action = 'NONE',
                       grant_refresh_deadline_at = NULL, progress_stage = %s,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE job_id = %s RETURNING updated_at""",
                (
                    new_revision,
                    next_token,
                    self._encrypt_grants(job_id, grants),
                    Jsonb(_without_grant_locators(prior | {"artifactAccessGrants": grants})),
                    "QUEUED" if mode == "REPLACE_QUEUED_GRANTS" else "RETRY_READY",
                    job_id,
                ),
            )
            updated_row = cursor.fetchone()
            if updated_row is None:
                raise RuntimeError("Grant revision update returned no row.")
            updated_at = cast(datetime, updated_row[0])
            receipt: dict[str, object] = {
                "jobId": job_id,
                "mode": mode,
                "grantRevision": new_revision,
                "attemptToken": next_token,
                "status": status,
                "updatedAt": updated_at.isoformat(),
            }
            cursor.execute(
                """INSERT INTO trading_review_grant_update_receipts
                       (idempotency_key_hash, job_id, request_fingerprint, receipt)
                   VALUES (%s, %s, %s, %s)""",
                (idempotency_key_hash, job_id, fingerprint, Jsonb(receipt)),
            )
        return _grant_receipt(receipt)

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
                        grant_action = 'NONE', grant_refresh_deadline_at = NULL,
                        current_grants_encrypted = NULL,
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
                        grant_action = 'NONE', grant_refresh_deadline_at = NULL,
                        current_grants_encrypted = NULL,
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
                SELECT job_id FROM trading_review_jobs
                WHERE status IN ('PENDING', 'RUNNING')
                  AND (
                      grant_action <> 'NONE'
                      OR (status = 'PENDING' AND EXISTS (
                          SELECT 1 FROM trading_review_grant_revisions AS g
                          WHERE g.job_id = trading_review_jobs.job_id
                            AND g.grant_revision = trading_review_jobs.grant_revision
                            AND (g.status <> 'ACTIVE'
                                 OR g.grant_expires_at <= CURRENT_TIMESTAMP)
                      ))
                      OR (lease_owner IS NOT NULL
                          AND lease_expires_at <= CURRENT_TIMESTAMP)
                  )
                ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED
                """
            )
            for (candidate_id,) in cursor.fetchall():
                self._reconcile_job(cursor, str(candidate_id))
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT j.job_id
                    FROM trading_review_jobs AS j
                    WHERE j.cancellation_requested = FALSE
                      AND j.core_normalization_run_id IS NOT NULL
                      AND j.result_delivery_policy IS NOT NULL
                      AND j.grant_action = 'NONE'
                      AND j.attempt_count < %s
                      AND (j.status = 'PENDING' OR
                           (j.status = 'RUNNING' AND j.lease_owner IS NULL))
                      AND (SELECT COUNT(*) FROM trading_review_grant_revisions AS g
                           WHERE g.job_id = j.job_id AND g.grant_revision = j.grant_revision
                             AND g.status = 'ACTIVE'
                             AND g.grant_expires_at > CURRENT_TIMESTAMP) = 2
                    ORDER BY j.created_at, j.job_id
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
                    j.request_payload, j.expected_input_set_hash, j.version_set_hash,
                    j.deletion_generation, j.attempt_token, j.attempt_count, j.created_at,
                    j.grant_revision, j.current_grants_encrypted, j.core_normalization_run_id,
                    j.computed_input_set_hash, j.hash_specification_version,
                    j.retry_policy_version, j.input_identity, j.result_delivery_policy
                """,
                (self._max_attempts, worker_id, lease_owner, lease_duration.total_seconds()),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            values = cast(
                tuple[
                    str,
                    str,
                    str,
                    dict[str, object],
                    str,
                    str,
                    int,
                    str,
                    int,
                    datetime,
                    int,
                    bytes | None,
                    str,
                    str | None,
                    str,
                    str,
                    dict[str, object],
                    dict[str, object],
                ],
                row,
            )
            request_payload = self._with_grant_locators(str(values[0]), values[3], values[11])
            request_payload["resultDeliveryPolicy"] = values[17]
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
            cursor.execute(
                """UPDATE trading_review_grant_revisions
                   SET status = 'CONSUMED', closed_at = CURRENT_TIMESTAMP
                   WHERE job_id = %s AND grant_revision = %s AND status = 'ACTIVE'""",
                (values[0], values[10]),
            )
            if cursor.rowcount != 2:
                raise RuntimeError("Claimed grants lost atomic freshness.")
            for grant in cast(
                list[dict[str, object]], request_payload.get("artifactAccessGrants", [])
            ):
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
            core_normalization_run_id=values[12],
            request_payload=request_payload,
            expected_input_set_hash=values[4],
            computed_input_set_hash=values[13],
            hash_specification_version=values[14],
            retry_policy_version=values[15],
            version_set_hash=values[5],
            deletion_generation=values[6],
            attempt_token=values[7],
            attempt_number=values[8],
            lease_owner=lease_owner,
            created_at=values[9],
            reclaimed_stale_lease=False,
            grant_revision=values[10],
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
        input_set_hash: str,
        reconciliation_manifest_hash: str,
        result_hash: str,
        retention: timedelta,
    ) -> bool:
        if retention <= timedelta(0):
            raise ValueError("retention must be positive.")
        policy = cast(dict[str, object], claim.request_payload["resultDeliveryPolicy"])
        limit, _limited_by = effective_payload_limit(policy, self._max_payload_bytes)
        if len(payload) > limit:
            raise ResultPayloadLimitExceeded()
        if hashlib.sha256(payload).hexdigest() != payload_sha256:
            raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH")
        document = parse_result_json_bytes(payload)
        raw_manifest = document.get("reconciliationManifest")
        manifest = cast(dict[str, object], raw_manifest) if isinstance(raw_manifest, dict) else None
        if (
            document.get("jobId") != claim.job_id
            or document.get("coreLogicalJobId") != claim.core_logical_job_id
            or document.get("coreImportSessionId") != claim.core_import_session_id
            or document.get("coreNormalizationRunId") != claim.core_normalization_run_id
            or document.get("attemptToken") != claim.attempt_token
            or document.get("deletionGeneration") != claim.deletion_generation
            or document.get("inputSetHash") != input_set_hash
            or document.get("hashSpecificationVersion") != claim.hash_specification_version
            or document.get("targetVersionSet")
            != cast(dict[str, object], claim.request_payload["inputIdentity"])["targetVersionSet"]
            or document.get("resultHash") != result_hash
            or manifest is None
            or manifest.get("manifestHash") != reconciliation_manifest_hash
        ):
            raise ValueError("Terminal payload identity differs from persisted job.")
        verify_public_result(document)
        counts = cast(dict[str, int], document["counts"])
        section_digests = cast(list[dict[str, object]], document["sectionDigests"])
        digest_manifest = cast(dict[str, object], document["resultDigestManifest"])
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
                SELECT cancellation_requested, grant_revision, expected_input_set_hash,
                       version_set_hash, computed_input_set_hash, core_normalization_run_id,
                       result_delivery_policy
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
            if (
                row[1] != claim.grant_revision
                or row[2] != input_set_hash
                or row[3] != claim.version_set_hash
                or row[4] != input_set_hash
                or row[5] != claim.core_normalization_run_id
                or row[6] != policy
            ):
                return False
            cursor.execute("SET CONSTRAINTS trading_review_jobs_terminal_payload_fk DEFERRED")
            cursor.execute(
                """
                INSERT INTO trading_review_terminal_payloads
                    (payload_id, job_id, attempt_number, attempt_token, deletion_generation,
                     media_type, payload_bytes, byte_size, payload_sha256, input_set_hash,
                     version_set_hash, reconciliation_manifest_hash, result_hash,
                     result_schema_version, result_hash_specification_version,
                     result_counts, section_digests, result_digest_manifest, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                    input_set_hash,
                    claim.version_set_hash,
                    reconciliation_manifest_hash,
                    result_hash,
                    document["resultSchemaVersion"],
                    document["resultHashSpecificationVersion"],
                    Jsonb(counts),
                    Jsonb(section_digests),
                    Jsonb(digest_manifest),
                    retention.total_seconds(),
                ),
            )
            cursor.execute(
                """
                UPDATE trading_review_jobs
                SET status = 'COMPLETED', progress_stage = 'COMPLETED',
                    terminal_payload_id = %s, result_hash = %s, failure = NULL,
                    terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                    current_grants_encrypted = NULL,
                    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, next_attempt_at = NULL
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP AND grant_revision = %s
                """,
                (payload_id, result_hash, *_claim_identity(claim), claim.grant_revision),
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
                    current_grants_encrypted = NULL,
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
        if claim.attempt_number >= self._max_attempts:
            return self.mark_failed(claim, failure)
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
                SET progress_stage = 'GRANT_REFRESH_WAIT', failure = NULL,
                    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = NULL, next_attempt_at = NULL,
                    grant_action = 'START_RETRY_ATTEMPT',
                    grant_refresh_deadline_at = LEAST(
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                        grant_renewal_cutoff_at),
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND status = 'RUNNING' AND lease_owner = %s
                  AND attempt_count = %s AND attempt_token = %s AND deletion_generation = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (self._grant_refresh_wait.total_seconds(), *_claim_identity(claim)),
            )
            if cursor.rowcount != 1:
                return False
            self._finish_attempt(cursor, claim, "RETRY")
        return True

    def get_result_descriptor(self, job_id: str) -> TerminalPayloadDescriptor | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.job_id, j.core_logical_job_id, j.core_normalization_run_id,
                       p.payload_id, p.media_type, p.byte_size, p.payload_sha256,
                       p.input_set_hash, j.hash_specification_version, j.target_version_set,
                       p.reconciliation_manifest_hash, p.result_hash, p.created_at,
                       p.expires_at, p.attempt_token, p.deletion_generation,
                       p.result_schema_version, p.result_hash_specification_version,
                       p.result_counts, p.section_digests, j.result_delivery_policy
                FROM trading_review_terminal_payloads AS p
                JOIN trading_review_jobs AS j ON j.job_id = p.job_id
                WHERE p.job_id = %s AND j.status = 'COMPLETED'
                  AND j.core_normalization_run_id IS NOT NULL AND p.input_set_hash IS NOT NULL
                  AND j.result_delivery_policy IS NOT NULL
                  AND p.expires_at > CURRENT_TIMESTAMP AND p.purged_at IS NULL
                  AND p.result_schema_version = 'TRADING_NORMALIZATION_RESULT_V1'
                """,
                (job_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if self._payload_cipher is None:
            raise RuntimeError("Terminal payload encryption is not configured.")
        values = cast(
            tuple[
                str,
                str,
                str,
                str,
                str,
                int,
                str,
                str,
                str,
                dict[str, object],
                str,
                str,
                datetime,
                datetime,
                str,
                int,
                str,
                str,
                dict[str, int],
                list[dict[str, object]],
                dict[str, object],
            ],
            row,
        )
        return TerminalPayloadDescriptor(
            job_id=str(values[0]),
            core_logical_job_id=values[1],
            core_normalization_run_id=values[2],
            payload_id=str(values[3]),
            media_type=values[4],
            byte_size=values[5],
            payload_sha256=values[6],
            input_set_hash=values[7],
            hash_specification_version=values[8],
            target_version_set=values[9],
            reconciliation_manifest_hash=values[10],
            result_hash=values[11],
            created_at=values[12],
            expires_at=values[13],
            attempt_token=values[14],
            deletion_generation=values[15],
            result_schema_version=values[16],
            result_hash_specification_version=values[17],
            counts=values[18],
            section_digests=values[19],
            result_delivery_policy_version=cast(str, values[20]["policyVersion"]),
            content_encoding=cast(list[str], values[20]["acceptedContentEncodings"])[0],
        )

    def get_terminal_payload(self, payload_id: str) -> bytes | None:
        cipher = self._payload_cipher
        if cipher is None:
            raise RuntimeError("Terminal payload encryption is not configured.")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.payload_bytes, p.payload_sha256, p.byte_size,
                       p.result_counts, p.section_digests, p.result_digest_manifest,
                       p.reconciliation_manifest_hash, p.result_hash
                FROM trading_review_terminal_payloads AS p
                JOIN trading_review_jobs AS j ON j.job_id = p.job_id
                WHERE p.payload_id = %s AND p.expires_at > CURRENT_TIMESTAMP
                  AND p.purged_at IS NULL AND j.status = 'COMPLETED'
                  AND j.core_normalization_run_id IS NOT NULL
                  AND p.result_schema_version = 'TRADING_NORMALIZATION_RESULT_V1'
                """,
                (payload_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        encrypted = bytes(row[0])
        if len(encrypted) < 28:
            raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH")
        try:
            payload = cipher.decrypt(encrypted[:12], encrypted[12:], payload_id.encode())
        except InvalidTag as error:
            raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH") from error
        if len(payload) != row[2] or hashlib.sha256(payload).hexdigest() != row[1]:
            raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH")
        document = parse_result_json_bytes(payload)
        verify_public_result(document)
        manifest = cast(dict[str, object], document["reconciliationManifest"])
        if document["counts"] != row[3]:
            raise ResultIntegrityError("RESULT_COUNT_MISMATCH")
        if document["sectionDigests"] != row[4]:
            raise ResultIntegrityError("RESULT_SECTION_HASH_MISMATCH")
        if document["resultDigestManifest"] != row[5] or document["resultHash"] != row[7]:
            raise ResultIntegrityError("RESULT_HASH_MISMATCH")
        if manifest["manifestHash"] != row[6]:
            raise ResultIntegrityError("RECONCILIATION_MANIFEST_HASH_MISMATCH")
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
        attempt_token: str,
        deletion_generation: int,
        disposition: str,
        recorded_at: datetime,
        current_deletion_generation: int | None,
    ) -> bool:
        fingerprint = _ack_fingerprint(
            core_aggregate_id,
            payload_id,
            payload_sha256,
            terminal_result_hash,
            attempt_token,
            deletion_generation,
            disposition,
            recorded_at,
            current_deletion_generation,
        )
        if disposition not in {"PERSISTED", "DISCARDED_STALE"} or (
            disposition == "DISCARDED_STALE"
            and (
                current_deletion_generation is None
                or current_deletion_generation <= deletion_generation
            )
        ):
            raise TradingReviewJobStateConflict("Invalid acknowledgement disposition.")
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
                SELECT j.core_import_session_id, j.status, p.payload_sha256, p.result_hash,
                       p.attempt_token, p.deletion_generation,
                       j.attempt_token, j.deletion_generation,
                       p.acknowledgement_disposition
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
            (
                aggregate_id,
                job_status,
                stored_payload_hash,
                stored_result_hash,
                stored_token,
                stored_generation,
                job_token,
                job_generation,
                stored_disposition,
            ) = cast(tuple[str, str, str, str, str, int, str, int, str | None], row)
            if (
                job_status != TradingReviewJobStatus.COMPLETED.value
                or aggregate_id != core_aggregate_id
                or stored_payload_hash != payload_sha256
                or stored_result_hash != terminal_result_hash
                or stored_token != attempt_token
                or stored_generation != deletion_generation
                or job_token != attempt_token
                or job_generation != deletion_generation
                or (stored_disposition is not None and stored_disposition != disposition)
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
                    , acknowledgement_disposition = COALESCE(
                        acknowledgement_disposition, %s)
                    , current_deletion_generation = COALESCE(
                        current_deletion_generation, %s)
                WHERE payload_id = %s AND job_id = %s
                """,
                (
                    core_aggregate_id,
                    recorded_at,
                    disposition,
                    current_deletion_generation,
                    payload_id,
                    job_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO trading_review_job_acknowledgements
                    (idempotency_key_hash, job_id, payload_id, request_fingerprint, expires_at)
                SELECT %s, j.job_id, %s, %s, j.terminal_at + INTERVAL '24 hours'
                FROM trading_review_jobs AS j WHERE j.job_id = %s
                """,
                (idempotency_key_hash, payload_id, fingerprint, job_id),
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
                    WHERE j.status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                      AND j.lease_owner IS NULL
                      AND j.terminal_at <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
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
                """WITH candidates AS (
                    SELECT payload_id FROM trading_review_terminal_payloads
                    WHERE purged_at IS NULL AND
                          (acknowledged_at IS NOT NULL OR expires_at <= CURRENT_TIMESTAMP)
                    ORDER BY acknowledged_at, expires_at, payload_id
                    LIMIT %s FOR UPDATE SKIP LOCKED
                )
                UPDATE trading_review_terminal_payloads AS p
                SET payload_bytes = NULL, purged_at = CURRENT_TIMESTAMP
                FROM candidates WHERE p.payload_id = candidates.payload_id""",
                (batch_size,),
            )
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
                grant_action = 'NONE', grant_refresh_deadline_at = NULL,
                current_grants_encrypted = NULL,
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
       j.terminal_payload_id, j.result_hash, j.cancellation_requested,
       j.grant_revision, j.expected_input_set_hash, j.version_set_hash,
       j.grant_action, j.grant_refresh_deadline_at,
       j.core_normalization_run_id, j.computed_input_set_hash,
       j.hash_specification_version, j.target_version_set, j.retry_policy_version
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
            int,
            str,
            str,
            str,
            datetime | None,
            str | None,
            str | None,
            str | None,
            dict[str, object] | None,
            str | None,
        ],
        row,
    )
    failure_data = values[13]
    if values[22] is None or values[24] is None or values[25] is None or values[26] is None:
        raise TradingReviewJobStateConflict("Legacy normalization identity is unavailable.")
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
        core_normalization_run_id=values[22],
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
        grant_revision=values[17],
        expected_input_set_hash=values[18],
        computed_input_set_hash=values[23],
        hash_specification_version=values[24],
        target_version_set=values[25],
        retry_policy_version=values[26],
        version_set_hash=values[19],
        grant_action=values[20],
        grant_refresh_deadline_at=values[21],
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
    attempt_token: str,
    deletion_generation: int,
    disposition: str,
    recorded_at: datetime,
    current_deletion_generation: int | None,
) -> str:
    payload = {
        "coreAggregateId": core_aggregate_id,
        "payloadId": payload_id,
        "payloadSha256": payload_sha256,
        "terminalResultHash": terminal_result_hash,
        "attemptToken": attempt_token,
        "deletionGeneration": deletion_generation,
        "disposition": disposition,
        "recordedAt": recorded_at.isoformat(),
        "currentDeletionGeneration": current_deletion_generation,
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


def _without_grant_locators(payload: dict[str, object]) -> dict[str, object]:
    return {
        **payload,
        "artifactAccessGrants": [
            {key: value for key, value in grant.items() if key != "accessUri"}
            for grant in cast(list[dict[str, object]], payload["artifactAccessGrants"])
        ],
    }


def _canonical_hash(payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _fresh_grants(grants: list[dict[str, object]], cursor: object) -> bool:
    if {str(item.get("role")) for item in grants} != {"TRADE_HISTORY", "POSITION_HISTORY"}:
        return False
    cursor.execute("SELECT CURRENT_TIMESTAMP")  # type: ignore[attr-defined]
    now = cursor.fetchone()[0]  # type: ignore[attr-defined]
    for grant in grants:
        try:
            expiry = datetime.fromisoformat(str(grant["expiresAt"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return False
        if expiry.tzinfo is None or not now < expiry <= now + timedelta(minutes=5):
            return False
    return True


def _insert_grant_revision(
    cursor: object, job_id: str, revision: int, token: str, grants: list[dict[str, object]]
) -> None:
    for grant in grants:
        cursor.execute(  # type: ignore[attr-defined]
            """INSERT INTO trading_review_grant_revisions
                   (job_id, grant_revision, artifact_role, core_artifact_id,
                    raw_sha256, grant_expires_at, attempt_token, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE')""",
            (
                job_id,
                revision,
                grant["role"],
                grant["coreArtifactId"],
                grant["rawSha256"],
                grant["expiresAt"],
                token,
            ),
        )


def _grant_receipt(payload: dict[str, object]) -> GrantUpdateReceipt:
    return GrantUpdateReceipt(
        str(payload["jobId"]),
        str(payload["mode"]),
        int(str(payload["grantRevision"])),
        str(payload["attemptToken"]),
        str(payload["status"]),
        datetime.fromisoformat(str(payload["updatedAt"]).replace("Z", "+00:00")),
    )
