"""PostgreSQL implementation of durable Backtest job runtime state."""

from datetime import date, datetime, timedelta
from typing import cast

from psycopg import connect
from psycopg.types.json import Jsonb

from refinvest.application.backtest_workers import ClaimedBacktestJob
from refinvest.application.backtests import (
    BacktestCapacityExceeded,
    BacktestIdempotencyConflict,
    BacktestJob,
    BacktestJobStatus,
    BacktestJobStatusView,
    BacktestSubmission,
)
from refinvest.engine.validation.models import Period


class PostgresBacktestJobRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def create(self, job: BacktestJob) -> None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO compute_backtest_jobs
                    (run_id, strategy_version_id, dataset_snapshot_id, status, request_payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    job.run_id,
                    job.strategy_version_id,
                    job.dataset_snapshot_id,
                    job.status.value,
                    Jsonb(job.request_payload),
                ),
            )

    def create_idempotently(
        self,
        job: BacktestJob,
        idempotency_key_hash: str,
        request_fingerprint: str,
        active_job_limit: int,
    ) -> BacktestSubmission:
        if active_job_limit <= 0:
            raise ValueError("active_job_limit must be positive.")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(734201891031)")
            cursor.execute(
                """
                SELECT run_id, strategy_version_id, request_fingerprint
                FROM compute_backtest_jobs
                WHERE idempotency_key_hash = %s
                """,
                (idempotency_key_hash,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                run_id, strategy_version_id, existing_fingerprint = cast(
                    tuple[str, str | None, str], existing
                )
                if existing_fingerprint != request_fingerprint:
                    raise BacktestIdempotencyConflict(
                        "Idempotency-Key was reused for a different request."
                    )
                return BacktestSubmission(str(run_id), strategy_version_id)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM compute_backtest_jobs
                WHERE status IN ('PENDING', 'RUNNING')
                """
            )
            active_job_count = cast(tuple[int], cursor.fetchone())[0]
            if active_job_count >= active_job_limit:
                raise BacktestCapacityExceeded("Compute active-job capacity is exhausted.")
            cursor.execute(
                """
                INSERT INTO compute_backtest_jobs
                    (run_id, strategy_version_id, dataset_snapshot_id, status, request_payload,
                     idempotency_key_hash, request_fingerprint)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING run_id, strategy_version_id
                """,
                (
                    job.run_id,
                    job.strategy_version_id,
                    job.dataset_snapshot_id,
                    job.status.value,
                    Jsonb(job.request_payload),
                    idempotency_key_hash,
                    request_fingerprint,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Idempotent backtest submission was not persisted.")
        run_id, strategy_version_id = cast(tuple[str, str | None], row)
        return BacktestSubmission(str(run_id), strategy_version_id)

    def get(self, run_id: str) -> BacktestJobStatusView | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, strategy_version_id,
                    COALESCE(resolved_dataset_snapshot_id, dataset_snapshot_id), status,
                    failure_reason, error_code, actual_period_start, actual_period_end,
                    engine_version, result_payload
                FROM compute_backtest_jobs
                WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        status_row = cast(
            tuple[
                str, str | None, str | None, str, str | None, str | None,
                object | None, object | None, str | None, dict[str, object] | None,
            ], row
        )
        (
            run_id_value,
            strategy_version_id,
            dataset_snapshot_id,
            status_value,
            failure_reason,
            error_code,
            actual_period_start,
            actual_period_end,
            engine_version,
            result_payload,
        ) = status_row
        return BacktestJobStatusView(
            run_id=str(run_id_value),
            strategy_version_id=strategy_version_id,
            dataset_snapshot_id=dataset_snapshot_id,
            status=BacktestJobStatus(status_value),
            failure_reason=failure_reason,
            error_code=error_code,
            actual_period=(
                Period(cast(date, actual_period_start), cast(date, actual_period_end))
                if actual_period_start is not None and actual_period_end is not None
                else None
            ),
            engine_version=engine_version,
            result_payload=result_payload,
        )

    def mark_completed(
        self,
        run_id: str,
        worker_id: str,
        result_payload: dict[str, object],
        actual_period: Period,
        engine_version: str,
    ) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE compute_backtest_jobs
                SET status = 'COMPLETED', result_payload = %s,
                    actual_period_start = %s, actual_period_end = %s, engine_version = %s,
                    completed_at = CURRENT_TIMESTAMP, terminal_at = CURRENT_TIMESTAMP,
                    failure_reason = NULL, error_code = NULL,
                    worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    next_attempt_at = NULL
                WHERE run_id = %s AND status = 'RUNNING' AND worker_id = %s
                """,
                (
                    Jsonb(result_payload), actual_period.start, actual_period.end, engine_version,
                    run_id, worker_id,
                ),
            )
            return cursor.rowcount == 1

    def pin_dataset_snapshot(self, run_id: str, snapshot_id: str) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE compute_backtest_jobs
                SET resolved_dataset_snapshot_id = %s
                WHERE run_id = %s AND status = 'PENDING' AND resolved_dataset_snapshot_id IS NULL
                """,
                (snapshot_id, run_id),
            )
            return cursor.rowcount == 1

    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> ClaimedBacktestJob | None:
        """Atomically lease one ready or stale job without allowing a status reversal."""
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT jobs.run_id, jobs.created_at,
                        (jobs.status = 'RUNNING' AND jobs.lease_expires_at <= CURRENT_TIMESTAMP)
                            AS reclaimed_stale_lease,
                        CASE
                            WHEN jobs.resolved_dataset_snapshot_id IS NOT NULL
                                THEN jobs.resolved_dataset_snapshot_id
                            WHEN jobs.dataset_snapshot_id IS NOT NULL
                                THEN requested.snapshot_id
                            ELSE latest.snapshot_id
                        END AS selected_snapshot_id
                    FROM compute_backtest_jobs AS jobs
                    LEFT JOIN compute_dataset_snapshots AS requested
                        ON requested.snapshot_id = jobs.dataset_snapshot_id
                    LEFT JOIN LATERAL (
                        SELECT snapshot_id
                        FROM compute_dataset_snapshots
                        ORDER BY created_at DESC, snapshot_id DESC
                        LIMIT 1
                    ) AS latest ON jobs.dataset_snapshot_id IS NULL
                    WHERE (
                        jobs.status = 'PENDING'
                        AND (
                            jobs.next_attempt_at IS NULL
                            OR jobs.next_attempt_at <= CURRENT_TIMESTAMP
                        )
                    ) OR (
                        jobs.status = 'RUNNING'
                        AND (
                            jobs.lease_expires_at <= CURRENT_TIMESTAMP
                            OR (
                                jobs.worker_id IS NULL
                                AND jobs.next_attempt_at <= CURRENT_TIMESTAMP
                            )
                        )
                    )
                    ORDER BY jobs.created_at, jobs.run_id
                    FOR UPDATE OF jobs SKIP LOCKED
                    LIMIT 1
                )
                UPDATE compute_backtest_jobs AS jobs
                SET status = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN 'FAILED'
                        ELSE 'RUNNING'
                    END,
                    resolved_dataset_snapshot_id = COALESCE(
                        jobs.resolved_dataset_snapshot_id, candidate.selected_snapshot_id
                    ),
                    worker_id = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN NULL ELSE %s
                    END,
                    lease_expires_at = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN NULL
                        ELSE CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
                    END,
                    heartbeat_at = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN NULL ELSE CURRENT_TIMESTAMP
                    END,
                    attempt_count = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN jobs.attempt_count
                        ELSE jobs.attempt_count + 1
                    END,
                    next_attempt_at = NULL,
                    terminal_at = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END,
                    failure_reason = CASE
                        WHEN candidate.selected_snapshot_id IS NULL
                            THEN 'No matching DatasetSnapshot is available.'
                        ELSE NULL
                    END,
                    error_code = CASE
                        WHEN candidate.selected_snapshot_id IS NULL THEN 'MISSING_REQUIRED_DATA'
                        ELSE NULL
                    END
                FROM candidate
                WHERE jobs.run_id = candidate.run_id
                RETURNING jobs.status, jobs.run_id, jobs.resolved_dataset_snapshot_id,
                    jobs.attempt_count, jobs.request_payload, candidate.created_at,
                    candidate.reclaimed_stale_lease
                """,
                (worker_id, lease_duration.total_seconds()),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        (
            status,
            run_id,
            dataset_snapshot_id,
            attempt_count,
            request_payload,
            created_at,
            reclaimed_stale_lease,
        ) = cast(tuple[str, str, str | None, int, dict[str, object], datetime, bool], row)
        if status != BacktestJobStatus.RUNNING.value or dataset_snapshot_id is None:
            return None
        return ClaimedBacktestJob(
            run_id=str(run_id),
            worker_id=worker_id,
            dataset_snapshot_id=dataset_snapshot_id,
            attempt_count=attempt_count,
            request_payload=request_payload,
            created_at=created_at,
            reclaimed_stale_lease=reclaimed_stale_lease,
        )

    def heartbeat(self, run_id: str, worker_id: str, lease_duration: timedelta) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE compute_backtest_jobs
                SET heartbeat_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
                WHERE run_id = %s AND status = 'RUNNING' AND worker_id = %s
                """,
                (lease_duration.total_seconds(), run_id, worker_id),
            )
            return cursor.rowcount == 1

    def mark_failed(
        self, run_id: str, worker_id: str, failure_reason: str, error_code: str | None
    ) -> bool:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE compute_backtest_jobs
                SET status = 'FAILED', terminal_at = CURRENT_TIMESTAMP,
                    failure_reason = %s, error_code = %s,
                    worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    next_attempt_at = NULL
                WHERE run_id = %s AND status = 'RUNNING' AND worker_id = %s
                """,
                (failure_reason, error_code, run_id, worker_id),
            )
            return cursor.rowcount == 1

    def delete_expired_terminal_jobs(self, retention: timedelta, batch_size: int) -> int:
        """Delete one bounded batch using the database clock, never active jobs."""
        if retention <= timedelta(0):
            raise ValueError("retention must be positive.")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT run_id
                    FROM compute_backtest_jobs
                    WHERE status IN ('COMPLETED', 'FAILED')
                      AND terminal_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                    ORDER BY terminal_at, run_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                DELETE FROM compute_backtest_jobs AS jobs
                USING expired
                WHERE jobs.run_id = expired.run_id
                """,
                (retention.total_seconds(), batch_size),
            )
            return cursor.rowcount

    def schedule_retry(
        self, run_id: str, worker_id: str, retry_delay: timedelta, failure_reason: str
    ) -> bool:
        """Keep RUNNING visible while releasing the lease for a bounded retry."""
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE compute_backtest_jobs
                SET worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    next_attempt_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    failure_reason = %s, error_code = NULL
                WHERE run_id = %s AND status = 'RUNNING' AND worker_id = %s
                """,
                (retry_delay.total_seconds(), failure_reason, run_id, worker_id),
            )
            return cursor.rowcount == 1
