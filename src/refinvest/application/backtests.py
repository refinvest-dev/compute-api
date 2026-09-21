"""Durable Backtest submission use case, independent of HTTP and PostgreSQL."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from refinvest.application.backtest_job_payload import (
    backtest_request_fingerprint,
    backtest_request_to_job_payload,
    idempotency_key_hash,
)
from refinvest.engine.validation import BacktestRequest, validate_strategy_version
from refinvest.engine.validation.models import Period


class BacktestJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BacktestJob:
    run_id: str
    strategy_version_id: str | None
    dataset_snapshot_id: str | None
    status: BacktestJobStatus
    request_payload: dict[str, object]


@dataclass(frozen=True)
class BacktestJobStatusView:
    run_id: str
    strategy_version_id: str | None
    dataset_snapshot_id: str | None
    status: BacktestJobStatus
    failure_reason: str | None = None
    error_code: str | None = None
    actual_period: Period | None = None
    engine_version: str | None = None
    result_payload: dict[str, object] | None = None


@dataclass(frozen=True)
class BacktestSubmission:
    """The stable original acceptance returned for an idempotent submission."""

    run_id: str
    strategy_version_id: str | None
    newly_created: bool = True


class BacktestIdempotencyConflict(ValueError):
    """One idempotency key was reused for a different canonical request."""


class BacktestCapacityExceeded(ValueError):
    """No global Compute execution capacity is available for a new submission."""


class BacktestJobRepository(Protocol):
    def create(self, job: BacktestJob) -> None: ...

    def create_idempotently(
        self,
        job: BacktestJob,
        idempotency_key_hash: str,
        request_fingerprint: str,
        active_job_limit: int,
    ) -> BacktestSubmission: ...

    def get(self, run_id: str) -> BacktestJobStatusView | None: ...


def submit_backtest(
    request: BacktestRequest,
    idempotency_key: str,
    repository: BacktestJobRepository,
    active_job_limit: int,
) -> BacktestSubmission:
    """Validate and durably accept a request without starting execution."""
    validate_strategy_version(request.strategy_version)
    request_payload = backtest_request_to_job_payload(request)
    job = BacktestJob(
        run_id=str(uuid4()),
        strategy_version_id=request.strategy_version_id,
        dataset_snapshot_id=request.dataset_snapshot_id,
        status=BacktestJobStatus.PENDING,
        request_payload=request_payload,
    )
    return repository.create_idempotently(
        job,
        idempotency_key_hash(idempotency_key),
        backtest_request_fingerprint(request_payload),
        active_job_limit,
    )
