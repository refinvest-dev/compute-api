"""Durable Backtest submission use case, independent of HTTP and PostgreSQL."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from refinvest.engine.validation import BacktestRequest, validate_strategy_version
from refinvest.engine.validation.models import Condition, MetricReference, Period, PositionPolicy


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


class BacktestJobRepository(Protocol):
    def create(self, job: BacktestJob) -> None: ...

    def get(self, run_id: str) -> BacktestJobStatusView | None: ...


def submit_backtest(
    request: BacktestRequest, repository: BacktestJobRepository
) -> BacktestJobStatusView:
    """Validate and durably accept a request without starting execution."""
    validate_strategy_version(request.strategy_version)
    job = BacktestJob(
        run_id=str(uuid4()),
        strategy_version_id=request.strategy_version_id,
        dataset_snapshot_id=request.dataset_snapshot_id,
        status=BacktestJobStatus.PENDING,
        request_payload=_request_payload(request),
    )
    repository.create(job)
    return _status_view(job)


def _status_view(job: BacktestJob) -> BacktestJobStatusView:
    return BacktestJobStatusView(
        job.run_id, job.strategy_version_id, job.dataset_snapshot_id, job.status
    )


def _request_payload(request: BacktestRequest) -> dict[str, object]:
    strategy = request.strategy_version
    return {
        "strategyVersionId": request.strategy_version_id,
        "strategyVersion": {
            "primarySignalAsset": strategy.primary_signal_asset.value,
            "conditions": [_condition_payload(condition) for condition in strategy.conditions],
            "executionAsset": strategy.execution_asset.value,
            "lag": strategy.lag,
            "exit": {"holdingSignalSessions": strategy.holding_signal_sessions},
            "positionPolicy": _position_policy_payload(strategy.position_policy),
        },
        "feeModel": {
            "commission": str(request.fee_model.commission),
            "slippage": str(request.fee_model.slippage),
        },
        "period": {
            "start": _json_value(request.period.start),
            "end": _json_value(request.period.end),
        },
        "datasetSnapshotId": request.dataset_snapshot_id,
    }


def _condition_payload(condition: Condition) -> dict[str, object]:
    operand_b: object = (
        _metric_payload(condition.operand_b)
        if isinstance(condition.operand_b, MetricReference)
        else str(condition.operand_b)
    )
    return {
        "operator": condition.operator.value,
        "logicalCombinator": _json_value(condition.logical_combinator),
        "operandA": _metric_payload(condition.operand_a),
        "operandB": operand_b,
    }


def _metric_payload(metric: MetricReference) -> dict[str, object]:
    return {"asset": metric.asset.value, "metric": metric.metric.value, "window": metric.window}


def _position_policy_payload(policy: PositionPolicy | None) -> dict[str, object] | None:
    if policy is None:
        return None
    return {
        "longOnly": policy.long_only,
        "singlePosition": policy.single_position,
        "duplicateEntry": _json_value(policy.duplicate_entry),
    }


def _json_value(value: date | datetime | StrEnum | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value.value
