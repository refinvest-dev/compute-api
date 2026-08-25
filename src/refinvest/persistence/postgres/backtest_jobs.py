"""PostgreSQL implementation of durable Backtest job runtime state."""

from typing import cast

from psycopg import connect
from psycopg.types.json import Jsonb

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
                SELECT run_id, strategy_version_id, dataset_snapshot_id, status
                FROM compute_backtest_jobs
                WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        run_id_value, strategy_version_id, dataset_snapshot_id, status_value = cast(
            tuple[str, str | None, str | None, str], row
        )
        return BacktestJobStatusView(
            run_id=run_id_value,
            strategy_version_id=strategy_version_id,
            dataset_snapshot_id=dataset_snapshot_id,
            status=BacktestJobStatus(status_value),
        )
