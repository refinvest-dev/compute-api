"""Durable Trading Review normalization job contracts.

This module deliberately contains no FastAPI, PostgreSQL, or reconstruction-engine code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import uuid4


class TradingReviewJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TradingReviewJobKind(StrEnum):
    NORMALIZATION_RECONCILIATION = "NORMALIZATION_RECONCILIATION"


@dataclass(frozen=True, slots=True)
class SafeTradingReviewFailure:
    code: str
    message: str
    retryable: bool
    stage: str
    details: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class TradingReviewJob:
    job_id: str
    core_logical_job_id: str
    core_import_session_id: str
    core_normalization_run_id: str
    kind: TradingReviewJobKind
    request_payload: dict[str, object]
    request_fingerprint: str
    expected_input_set_hash: str
    computed_input_set_hash: str | None
    hash_specification_version: str
    retry_policy_version: str
    version_set_hash: str
    deletion_generation: int
    attempt_token: str


@dataclass(frozen=True, slots=True)
class TradingReviewJobSubmission:
    job_id: str
    core_logical_job_id: str
    kind: TradingReviewJobKind
    created_at: datetime
    newly_created: bool = True


@dataclass(frozen=True, slots=True)
class TerminalPayloadDescriptor:
    job_id: str
    core_logical_job_id: str
    core_normalization_run_id: str
    payload_id: str
    media_type: str
    byte_size: int
    payload_sha256: str
    input_set_hash: str
    hash_specification_version: str
    target_version_set: dict[str, object]
    reconciliation_manifest_hash: str
    result_hash: str
    created_at: datetime
    expires_at: datetime
    attempt_token: str = ""
    deletion_generation: int = 0


@dataclass(frozen=True, slots=True)
class TradingReviewJobStatusView:
    job_id: str
    core_logical_job_id: str
    core_import_session_id: str
    core_normalization_run_id: str
    kind: TradingReviewJobKind
    status: TradingReviewJobStatus
    progress_stage: str
    attempt: int
    attempt_token: str
    deletion_generation: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    terminal_at: datetime | None = None
    failure: SafeTradingReviewFailure | None = None
    terminal_payload_id: str | None = None
    result_hash: str | None = None
    cancellation_requested: bool = False
    grant_revision: int = 0
    expected_input_set_hash: str = ""
    computed_input_set_hash: str | None = None
    hash_specification_version: str = ""
    target_version_set: dict[str, object] | None = None
    retry_policy_version: str = ""
    version_set_hash: str = ""
    grant_action: str = "NONE"
    grant_refresh_deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClaimedTradingReviewJob:
    job_id: str
    core_logical_job_id: str
    core_import_session_id: str
    core_normalization_run_id: str
    request_payload: dict[str, object]
    expected_input_set_hash: str
    computed_input_set_hash: str | None
    hash_specification_version: str
    retry_policy_version: str
    version_set_hash: str
    deletion_generation: int
    attempt_token: str
    attempt_number: int
    lease_owner: str
    created_at: datetime
    reclaimed_stale_lease: bool
    grant_revision: int = 0


class TradingReviewIdempotencyConflict(ValueError):
    """One idempotency key was reused for a different canonical request."""


class TradingReviewJobStateConflict(ValueError):
    """A command does not match the persisted job/payload state."""


class TradingReviewCapacityExceeded(ValueError):
    """The dedicated Trading Review active-job pool is full."""


class TradingReviewGrantInvalid(ValueError):
    """A newly supplied artifact grant is expired or otherwise unusable."""


@dataclass(frozen=True, slots=True)
class GrantUpdateReceipt:
    job_id: str
    mode: str
    grant_revision: int
    attempt_token: str
    status: str
    updated_at: datetime


class TradingReviewJobRepository(Protocol):
    def create_idempotently(
        self, job: TradingReviewJob, idempotency_key_hash: str
    ) -> TradingReviewJobSubmission: ...

    def get(self, job_id: str) -> TradingReviewJobStatusView | None: ...

    def update_grants(
        self, job_id: str, idempotency_key_hash: str, payload: dict[str, object]
    ) -> GrantUpdateReceipt | None: ...

    def request_cancellation(
        self, job_id: str, idempotency_key_hash: str
    ) -> TradingReviewJobStatusView | None: ...

    def get_result_descriptor(self, job_id: str) -> TerminalPayloadDescriptor | None: ...

    def get_terminal_payload(self, payload_id: str) -> bytes | None: ...

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
    ) -> bool: ...


class TradingReviewWorkerRepository(Protocol):
    def claim_next(
        self, worker_id: str, lease_duration: timedelta
    ) -> ClaimedTradingReviewJob | None: ...

    def heartbeat(
        self,
        job_id: str,
        lease_owner: str,
        attempt_token: str,
        deletion_generation: int,
        lease_duration: timedelta,
    ) -> bool: ...

    def cancellation_requested(
        self,
        job_id: str,
        lease_owner: str,
        attempt_token: str,
        deletion_generation: int,
    ) -> bool: ...

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
    ) -> bool: ...

    def mark_failed(
        self, claim: ClaimedTradingReviewJob, failure: SafeTradingReviewFailure
    ) -> bool: ...

    def mark_cancelled(self, claim: ClaimedTradingReviewJob) -> bool: ...

    def schedule_retry(
        self,
        claim: ClaimedTradingReviewJob,
        delay: timedelta,
        failure: SafeTradingReviewFailure,
    ) -> bool: ...


def submit_trading_review_job(
    request_payload: dict[str, object],
    idempotency_key: str,
    repository: TradingReviewJobRepository,
    computed_input_set_hash: str | None,
) -> TradingReviewJobSubmission:
    """Atomically admit one exact, immutable normalization request."""
    fingerprint = normalization_request_fingerprint(request_payload)
    identity = cast(dict[str, object], request_payload["inputIdentity"])
    job = TradingReviewJob(
        job_id=str(uuid4()),
        core_logical_job_id=_required_string(request_payload, "coreLogicalJobId"),
        core_import_session_id=_required_string(request_payload, "coreImportSessionId"),
        core_normalization_run_id=_required_string(request_payload, "coreNormalizationRunId"),
        kind=TradingReviewJobKind.NORMALIZATION_RECONCILIATION,
        request_payload=request_payload,
        request_fingerprint=fingerprint,
        expected_input_set_hash=_required_string(request_payload, "expectedInputSetHash"),
        computed_input_set_hash=computed_input_set_hash,
        hash_specification_version=_required_string(identity, "hashSpecificationVersion"),
        retry_policy_version=_required_string(request_payload, "retryPolicyVersion"),
        version_set_hash=_required_nested_string(
            cast(dict[str, object], request_payload["inputIdentity"]),
            "targetVersionSet",
            "versionSetHash",
        ),
        deletion_generation=_required_integer(request_payload, "deletionGeneration"),
        attempt_token=_required_string(request_payload, "attemptToken"),
    )
    return repository.create_idempotently(job, hash_idempotency_key(idempotency_key))


def normalization_request_fingerprint(payload: dict[str, object]) -> str:
    """Hash semantic immutable input while excluding expiring access capabilities."""
    canonical = cast(dict[str, object], json.loads(json.dumps(payload)))
    artifacts_node = canonical.get("artifactAccessGrants")
    if isinstance(artifacts_node, list):
        artifacts = cast(list[dict[str, object]], artifacts_node)
        for artifact in artifacts:
            artifact.pop("accessUri", None)
            artifact.pop("expiresAt", None)
        artifacts.sort(key=lambda item: str(item.get("role", "")))
    terms_node = canonical.get("pinnedInstrumentTerms")
    if isinstance(terms_node, list):
        terms = cast(list[dict[str, object]], terms_node)
        terms.sort(key=lambda item: str(item.get("instrumentId", "")))
    encoded = json.dumps(
        canonical, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def hash_idempotency_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _required_integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    return value


def _required_nested_string(payload: dict[str, object], parent: str, name: str) -> str:
    value = payload.get(parent)
    if not isinstance(value, dict):
        raise ValueError(f"{parent} must be an object.")
    return _required_string(cast(dict[str, object], value), name)
