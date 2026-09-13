from datetime import date
from decimal import Decimal

from refinvest.application.backtest_job_payload import (
    backtest_request_fingerprint,
    backtest_request_from_job_payload,
    backtest_request_to_job_payload,
    idempotency_key_hash,
)
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


def test_request_uses_the_canonical_durable_job_payload() -> None:
    request = _request()

    payload = backtest_request_to_job_payload(request)

    assert payload == _expected_payload()
    assert backtest_request_from_job_payload(payload) == request


def test_canonical_request_and_idempotency_key_hashes_are_stable() -> None:
    payload = backtest_request_to_job_payload(_request())

    assert (
        backtest_request_fingerprint(payload)
        == "88879c826631e54fc010587c867ca50e4bd320930d9c9ed96da9a2c128ce3bd9"
    )
    assert (
        idempotency_key_hash("d9428888-122b-4f8a-8a95-2c5b31eb3380")
        == "d0a11e3535cbe80330b1f72021258617f056a253a5ddc1d7da33edbbea5720f6"
    )


def _request() -> BacktestRequest:
    return BacktestRequest(
        strategy_version_id="strategy-v1",
        strategy_version=StrategyVersion(
            primary_signal_asset=AssetSymbol.BTCUSDT,
            conditions=(
                Condition(
                    ComparisonOperator.GT,
                    None,
                    MetricReference(AssetSymbol.BTCUSDT, MetricType.RETURN, 2),
                    Decimal("0.0100"),
                ),
                Condition(
                    ComparisonOperator.LTE,
                    LogicalCombinator.AND,
                    MetricReference(AssetSymbol.QQQ, MetricType.CHANGE, 3),
                    MetricReference(AssetSymbol.SPY, MetricType.SIMPLE, None),
                ),
            ),
            execution_asset=AssetSymbol.TQQQ,
            lag=1,
            holding_signal_sessions=4,
            position_policy=PositionPolicy(
                True,
                True,
                DuplicateEntryPolicy.IGNORE,
            ),
        ),
        fee_model=FeeModel(Decimal("0.0010"), Decimal("0.0025")),
        period=Period(date(2025, 1, 2), date(2025, 1, 31)),
        dataset_snapshot_id="snapshot-1",
    )


def _expected_payload() -> dict[str, object]:
    return {
        "strategyVersionId": "strategy-v1",
        "strategyVersion": {
            "primarySignalAsset": "BTCUSDT",
            "conditions": [
                {
                    "operator": "GT",
                    "logicalCombinator": None,
                    "operandA": {"asset": "BTCUSDT", "metric": "RETURN", "window": 2},
                    "operandB": "0.0100",
                },
                {
                    "operator": "LTE",
                    "logicalCombinator": "AND",
                    "operandA": {"asset": "QQQ", "metric": "CHANGE", "window": 3},
                    "operandB": {"asset": "SPY", "metric": "SIMPLE", "window": None},
                },
            ],
            "executionAsset": "TQQQ",
            "lag": 1,
            "exit": {"holdingSignalSessions": 4},
            "positionPolicy": {
                "longOnly": True,
                "singlePosition": True,
                "duplicateEntry": "IGNORE",
            },
        },
        "feeModel": {"commission": "0.0010", "slippage": "0.0025"},
        "period": {"start": "2025-01-02", "end": "2025-01-31"},
        "datasetSnapshotId": "snapshot-1",
    }
