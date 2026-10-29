"""Analytics persisted document projections, independent of database connections."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from refinvest.application.analytics_jobs import AnalyticsJobConflict


def stable_request(request: dict[str, object]) -> dict[str, object]:
    stable = dict(request)
    for role in ("ledgerRevisionContent", "reconciliationManifest"):
        reference = cast(dict[str, object], stable[role])
        stable[role] = {
            key: value for key, value in reference.items() if key not in {"accessUri", "expiresAt"}
        }
    return stable


def grants_from_request(request: dict[str, object]) -> dict[str, object]:
    return {
        role: {
            "accessUri": cast(dict[str, object], request[role])["accessUri"],
            "expiresAt": cast(dict[str, object], request[role])["expiresAt"],
        }
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    }


def expiry(grants: dict[str, object], now: datetime) -> datetime:
    expiries = [
        datetime.fromisoformat(
            str(cast(dict[str, object], grants[role])["expiresAt"]).replace("Z", "+00:00")
        )
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    ]
    if any(value <= now or value > now + timedelta(minutes=5) for value in expiries):
        raise AnalyticsJobConflict("INPUT_REFERENCE_EXPIRED")
    return min(expiries)


def status_row(row: dict[str, Any]) -> dict[str, object]:
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


def accepted_row(row: dict[str, Any]) -> dict[str, object]:
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


def build_terminal_descriptor(
    claim: dict[str, object],
    payload: dict[str, object],
    *,
    payload_id: str,
    body_size: int,
    payload_sha256: str,
    created_at: datetime,
    expires_at: datetime,
) -> dict[str, object]:
    request = cast(dict[str, object], claim["request"])
    return {
        "jobId": claim["jobId"],
        "coreLogicalJobId": request["coreLogicalJobId"],
        "coreAnalysisRunId": request["coreAnalysisRunId"],
        "payloadId": payload_id,
        "transportMode": "SINGLE_RESOURCE",
        "resultDeliveryPolicyVersion": "ANALYTICS_RESULT_DELIVERY_V1",
        "mediaType": "application/vnd.refinvest.trading-analytics+json",
        "contentEncoding": "identity",
        "byteSize": body_size,
        "payloadSha256": payload_sha256,
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
