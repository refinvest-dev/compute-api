"""Normalization PostgreSQL row projections and persisted document serialization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import cast

from refinvest.application.trading_review_jobs import (
    ClaimedTradingReviewJob,
    GrantUpdateReceipt,
    SafeTradingReviewFailure,
    TerminalPayloadDescriptor,
    TradingReviewJobKind,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    TradingReviewJobStatusView,
)


def status_view(row: object | None) -> TradingReviewJobStatusView | None:
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
            str,
            str,
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
        result_schema_version=values[27],
        result_hash_specification_version=values[28],
    )


def claim_identity(claim: ClaimedTradingReviewJob) -> tuple[object, ...]:
    return (
        claim.job_id,
        claim.lease_owner,
        claim.attempt_number,
        claim.attempt_token,
        claim.deletion_generation,
    )


def failure_json(failure: SafeTradingReviewFailure) -> dict[str, object]:
    return {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
        "stage": failure.stage,
        "details": list(failure.details),
    }


def ack_fingerprint(
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


def without_grant_locators(payload: dict[str, object]) -> dict[str, object]:
    return {
        **payload,
        "artifactAccessGrants": [
            {key: value for key, value in grant.items() if key != "accessUri"}
            for grant in cast(list[dict[str, object]], payload["artifactAccessGrants"])
        ],
    }


def canonical_hash(payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def grant_receipt(payload: dict[str, object]) -> GrantUpdateReceipt:
    return GrantUpdateReceipt(
        str(payload["jobId"]),
        str(payload["mode"]),
        int(str(payload["grantRevision"])),
        str(payload["attemptToken"]),
        str(payload["status"]),
        datetime.fromisoformat(str(payload["updatedAt"]).replace("Z", "+00:00")),
    )


def terminal_descriptor(row: object) -> TerminalPayloadDescriptor:
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
