"""Normalization grant SQL on the repository-owned transaction cursor."""

from __future__ import annotations

from datetime import datetime, timedelta


def fresh_grants(grants: list[dict[str, object]], cursor: object) -> bool:
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


def insert_grant_revision(
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


def close_artifact_leases(cursor: object, job_id: str, attempt_number: int, status: str) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        """
        UPDATE trading_review_artifact_leases
        SET status = %s, closed_at = CURRENT_TIMESTAMP
        WHERE job_id = %s AND attempt_number = %s AND status = 'ACTIVE'
        """,
        (status, job_id, attempt_number),
    )
