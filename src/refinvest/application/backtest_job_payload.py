"""Canonical durable payload mapping for Backtest jobs."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from refinvest.engine.validation.models import (
    AssetSymbol,
    BacktestRequest,
    ComparisonOperator,
    Condition,
    DuplicateEntryPolicy,
    FeeModel,
    LogicalCombinator,
    MetricReference,
    MetricType,
    Period,
    PositionPolicy,
    StrategyVersion,
)


class BacktestJobPayloadError(ValueError):
    """A stored durable Job payload cannot be restored safely."""


def backtest_request_to_job_payload(request: BacktestRequest) -> dict[str, object]:
    """Encode a request using the canonical durable Job representation."""
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


def backtest_request_from_job_payload(payload: dict[str, object]) -> BacktestRequest:
    """Restore a Backtest request from its canonical durable Job representation."""
    strategy_payload = _mapping(payload.get("strategyVersion"), "strategyVersion")
    fee_payload = _mapping(payload.get("feeModel"), "feeModel")
    period_payload = _mapping(payload.get("period"), "period")
    condition_values = _list(strategy_payload, "conditions")
    conditions = tuple(_condition_from_payload(value) for value in condition_values)
    policy_value = strategy_payload.get("positionPolicy")
    policy = (
        None
        if policy_value is None
        else _policy_from_payload(_mapping(policy_value, "positionPolicy"))
    )
    try:
        return BacktestRequest(
            strategy_version_id=_optional_string(payload.get("strategyVersionId")),
            strategy_version=StrategyVersion(
                primary_signal_asset=AssetSymbol(_string(strategy_payload, "primarySignalAsset")),
                conditions=conditions,
                execution_asset=AssetSymbol(_string(strategy_payload, "executionAsset")),
                lag=_integer(strategy_payload, "lag"),
                holding_signal_sessions=_integer(
                    _mapping(strategy_payload.get("exit"), "exit"), "holdingSignalSessions"
                ),
                position_policy=policy,
            ),
            fee_model=FeeModel(
                commission=_decimal(fee_payload, "commission"),
                slippage=_decimal(fee_payload, "slippage"),
            ),
            period=Period(
                date.fromisoformat(_string(period_payload, "start")),
                date.fromisoformat(_string(period_payload, "end")),
            ),
            dataset_snapshot_id=_optional_string(payload.get("datasetSnapshotId")),
        )
    except (TypeError, ValueError) as error:
        raise BacktestJobPayloadError("Stored backtest request is invalid.") from error


def idempotency_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def backtest_request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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


def _condition_from_payload(value: object) -> Condition:
    payload = _mapping(value, "condition")
    combinator = payload.get("logicalCombinator")
    return Condition(
        ComparisonOperator(_string(payload, "operator")),
        LogicalCombinator(combinator) if isinstance(combinator, str) else None,
        _metric_from_payload(_mapping(payload.get("operandA"), "operandA")),
        _operand_from_payload(payload.get("operandB")),
    )


def _operand_from_payload(value: object) -> MetricReference | Decimal:
    if isinstance(value, dict):
        return _metric_from_payload(cast(dict[str, object], value))
    if isinstance(value, str):
        return Decimal(value)
    raise BacktestJobPayloadError("Stored condition operandB is invalid.")


def _metric_from_payload(payload: dict[str, object]) -> MetricReference:
    window = payload.get("window")
    return MetricReference(
        AssetSymbol(_string(payload, "asset")),
        MetricType(_string(payload, "metric")),
        window if isinstance(window, int) and not isinstance(window, bool) else None,
    )


def _policy_from_payload(payload: dict[str, object]) -> PositionPolicy:
    duplicate_entry = payload.get("duplicateEntry")
    return PositionPolicy(
        _optional_bool(payload.get("longOnly")),
        _optional_bool(payload.get("singlePosition")),
        DuplicateEntryPolicy(duplicate_entry) if isinstance(duplicate_entry, str) else None,
    )


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BacktestJobPayloadError(f"Stored {field} must be an object.")
    return cast(dict[str, object], value)


def _list(payload: dict[str, object], field: str) -> list[object]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise BacktestJobPayloadError(f"Stored {field} must be an array.")
    return cast(list[object], value)


def _string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise BacktestJobPayloadError(f"Stored {field} must be a string.")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise BacktestJobPayloadError("Stored optional identifier must be a string.")


def _integer(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BacktestJobPayloadError(f"Stored {field} must be an integer.")
    return value


def _decimal(payload: dict[str, object], field: str) -> Decimal:
    value = _string(payload, field)
    try:
        return Decimal(value)
    except ValueError as error:
        raise BacktestJobPayloadError(f"Stored {field} must be a Decimal string.") from error


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
