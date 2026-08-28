"""PostgreSQL implementation of durable Backtest job runtime state."""

from datetime import timedelta
from typing import cast

from psycopg import connect
from psycopg.types.json import Jsonb

from refinvest.application.backtest_workers import ClaimedBacktestJob
from refinvest.application.backtests import BacktestJob, BacktestJobStatus, BacktestJobStatusView


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

    def get(self, run_id: str) -> BacktestJobStatusView | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, strategy_version_id,
                    COALESCE(resolved_dataset_snapshot_id, dataset_snapshot_id), status,
                    failure_reason, error_code
                FROM compute_backtest_jobs
                WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        status_row = cast(
            tuple[str, str | None, str | None, str, str | None, str | None], row
        )
        (
            run_id_value,
            strategy_version_id,
            dataset_snapshot_id,
            status_value,
            failure_reason,
            error_code,
        ) = status_row
        return BacktestJobStatusView(
            run_id=str(run_id_value),
            strategy_version_id=strategy_version_id,
            dataset_snapshot_id=dataset_snapshot_id,
            status=BacktestJobStatus(status_value),
            failure_reason=failure_reason,
            error_code=error_code,
        )

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
                    SELECT jobs.run_id,
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
                    jobs.attempt_count, jobs.request_payload
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
        ) = cast(tuple[str, str, str | None, int, dict[str, object]], row)
        if status != BacktestJobStatus.RUNNING.value or dataset_snapshot_id is None:
            return None
        return ClaimedBacktestJob(
            run_id=str(run_id),
            worker_id=worker_id,
            dataset_snapshot_id=dataset_snapshot_id,
            attempt_count=attempt_count,
            request_payload=request_payload,
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
                SET status = 'FAILED', failure_reason = %s, error_code = %s,
                    worker_id = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    next_attempt_at = NULL
                WHERE run_id = %s AND status = 'RUNNING' AND worker_id = %s
                """,
                (failure_reason, error_code, run_id, worker_id),
            )
            return cursor.rowcount == 1

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
