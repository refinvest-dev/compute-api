"""Serialize a permanent Run fence with create; verify resources before deleting live rows."""

from __future__ import annotations

from socket import gethostname

from psycopg import connect

from refinvest.adapters.normalization.process_identity import process_identity, terminate_process
from refinvest.application.normalization_deletion import (
    DeleteNormalizationRuntime,
    NormalizationRuntimeAbsence,
)
from refinvest.application.trading_review_jobs import TradingReviewJobStateConflict


def normalization_run_lock(cursor: object, run_id: str) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("normalization-run:" + run_id,)
    )


class PostgresNormalizationRuntimeDeletion:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def delete_expired_receipts(self, limit: int) -> int:
        if limit <= 0:
            raise ValueError("Cleanup limit must be positive")
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT core_normalization_run_id FROM trading_normalization_deletions
                WHERE verified_absent_at < CURRENT_TIMESTAMP - INTERVAL '180 days'
                ORDER BY verified_absent_at LIMIT %s""",
                (limit,),
            )
            candidates = [row[0] for row in cursor.fetchall()]
            deleted = 0
            for run_id in candidates:
                normalization_run_lock(cursor, run_id)
                cursor.execute(
                    """DELETE FROM trading_normalization_deletions d
                    WHERE d.core_normalization_run_id=%s
                    AND d.verified_absent_at < CURRENT_TIMESTAMP - INTERVAL '180 days'
                    AND NOT EXISTS (SELECT 1 FROM trading_review_jobs j
                        WHERE j.core_normalization_run_id=d.core_normalization_run_id)""",
                    (run_id,),
                )
                deleted += cursor.rowcount
            return deleted

    def purge(self, command: DeleteNormalizationRuntime) -> NormalizationRuntimeAbsence:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            normalization_run_lock(cursor, command.run_id)
            cursor.execute(
                """
            SELECT core_import_session_id,source_deletion_generation FROM
            trading_normalization_deletions WHERE core_normalization_run_id=%s
            """,
                (command.run_id,),
            )
            saved = cursor.fetchone()
            if saved is not None and saved != (command.session_id, command.source_generation):
                raise TradingReviewJobStateConflict("Cleanup source identity conflict")
            cursor.execute(
                """
            SELECT deletion_generation FROM trading_normalization_deletion_receipts
            WHERE core_normalization_run_id=%s AND deletion_request_id=%s
            """,
                (command.run_id, command.request_id),
            )
            receipt = cursor.fetchone()
            if receipt is not None and receipt[0] != command.generation:
                raise TradingReviewJobStateConflict("Cleanup request generation conflict")
            cursor.execute(
                """
            SELECT job_id,core_import_session_id,deletion_generation FROM
            trading_review_jobs WHERE core_normalization_run_id=%s FOR UPDATE
            """,
                (command.run_id,),
            )
            jobs = cursor.fetchall()
            if any(row[1:] != (command.session_id, command.source_generation) for row in jobs):
                raise TradingReviewJobStateConflict("Cleanup job identity conflict")
            cursor.execute(
                """
            INSERT INTO
            trading_normalization_deletions(core_normalization_run_id,core_import_session_id,source_deletion_generation)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """,
                (command.run_id, command.session_id, command.source_generation),
            )
            cursor.execute(
                """
            INSERT INTO
            trading_normalization_deletion_receipts(core_normalization_run_id,deletion_request_id,deletion_generation)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
            """,
                (command.run_id, command.request_id, command.generation),
            )
            cursor.execute(
                """
            UPDATE trading_review_jobs SET
            cancellation_requested=TRUE,current_grants_encrypted=NULL,
            grant_action='NONE',grant_refresh_deadline_at=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE core_normalization_run_id=%s
            """,
                (command.run_id,),
            )
            cursor.execute(
                """
            SELECT
            e.job_id,e.lease_owner,e.host_name,e.parent_pid,e.parent_identity,e.child_pid,e.child_identity
            FROM trading_normalization_executions e JOIN trading_review_jobs j ON
            j.job_id=e.job_id
            WHERE j.core_normalization_run_id=%s AND e.released_at IS NULL FOR UPDATE OF
            e
            """,
                (command.run_id,),
            )
            live = False
            for (
                job_id,
                owner,
                host,
                parent_pid,
                parent_birth,
                child_pid,
                child_birth,
            ) in cursor.fetchall():
                absent = False
                if host == gethostname():
                    try:
                        if child_pid is not None and child_birth is not None:
                            parent_absent = (
                                parent_pid is not None
                                and parent_birth is not None
                                and process_identity(parent_pid) != parent_birth
                            )
                            if parent_absent:
                                absent = process_identity(child_pid) != child_birth
                                if not absent:
                                    terminate_process(child_pid, child_birth)
                            # A living parent may still hold the returned payload after child exit.
                            # Its explicit memory release is required before closing the guard.
                        elif parent_pid is not None and parent_birth is not None:
                            # No child was permitted to fetch bytes before binding its identity.
                            absent = process_identity(parent_pid) != parent_birth
                    except OSError:
                        absent = False
                if absent:
                    cursor.execute(
                        """
            UPDATE trading_normalization_executions SET released_at=CURRENT_TIMESTAMP
            WHERE job_id=%s AND lease_owner=%s
            """,
                        (job_id, owner),
                    )
                else:
                    live = True
            if not live:
                cursor.execute(
                    """
            DELETE FROM trading_review_job_acknowledgements WHERE job_id IN (SELECT
            job_id FROM trading_review_jobs WHERE core_normalization_run_id=%s)
            """,
                    (command.run_id,),
                )
                cursor.execute(
                    "DELETE FROM trading_review_jobs WHERE core_normalization_run_id=%s",
                    (command.run_id,),
                )
                cursor.execute(
                    """
            UPDATE trading_normalization_deletions SET
            verified_absent_at=COALESCE(verified_absent_at,CURRENT_TIMESTAMP),
            last_confirmed_at=CURRENT_TIMESTAMP WHERE core_normalization_run_id=%s
            RETURNING verified_absent_at
            """,
                    (command.run_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                return NormalizationRuntimeAbsence(command, row[0])
            return NormalizationRuntimeAbsence(command, None)
