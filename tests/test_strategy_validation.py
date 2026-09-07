from decimal import Decimal

import pytest

from refinvest.api.schemas.backtests import CreateBacktestRequestTransport
from refinvest.api.schemas.errors import map_strategy_validation_error
from refinvest.engine.validation import (
    StrategyValidationError,
    ValidationIssue,
    validate_strategy_version,
)
from refinvest.engine.validation.models import (
    AssetSymbol,
    ComparisonOperator,
    Condition,
    LogicalCombinator,
    MetricReference,
    MetricType,
    PositionPolicy,
    StrategyVersion,
)


def strategy_with(condition: dict[str, object]) -> dict[str, object]:
    return {
        "primarySignalAsset": "QQQ",
        "conditions": [condition],
        "executionAsset": "TQQQ",
        "lag": 0,
        "exit": {"holdingSignalSessions": 1},
    }


def test_validates_documented_metric_window_and_condition_order_rules() -> None:
    request = CreateBacktestRequestTransport.model_validate(
        {
            "strategyVersion": strategy_with(
                {
                    "operator": "GT",
                    "logicalCombinator": "AND",
                    "operandA": {"asset": "QQQ", "metric": "SIMPLE", "window": 1},
                    "operandB": 0.0,
                }
            ),
            "feeModel": {"commission": 0.0, "slippage": 0.0},
            "period": {"start": "2024-01-01", "end": "2024-12-31"},
        }
    ).to_domain()

    with pytest.raises(StrategyValidationError) as raised:
        validate_strategy_version(request.strategy_version)

    response = map_strategy_validation_error(raised.value)
    assert response.error_code == "DSL_INVALID"
    assert response.details == [
        "conditions[0].logicalCombinator: The first condition must not have a combinator.",
        "conditions[0].operandA.window: SIMPLE metrics must not define a window.",
    ]


def test_allows_primary_signal_asset_without_a_condition_reference() -> None:
    request = CreateBacktestRequestTransport.model_validate(
        {
            "strategyVersion": strategy_with(
                {
                    "operator": "LTE",
                    "operandA": {"asset": "SPY", "metric": "SIMPLE"},
                    "operandB": 20.0,
                }
            ),
            "feeModel": {"commission": 0.0, "slippage": 0.0},
            "period": {"start": "2024-01-01", "end": "2024-12-31"},
        }
    ).to_domain()

    validate_strategy_version(request.strategy_version)


def test_pure_validator_rejects_metric_and_timing_invariants() -> None:
    strategy = StrategyVersion(
        primary_signal_asset=AssetSymbol.QQQ,
        conditions=(
            Condition(
                operator=ComparisonOperator.GT,
                logical_combinator=None,
                operand_a=MetricReference(
                    asset=AssetSymbol.QQQ,
                    metric=MetricType.RETURN,
                    window=None,
                ),
                operand_b=0.0,
            ),
        ),
        execution_asset=AssetSymbol.TQQQ,
        lag=-1,
        holding_signal_sessions=0,
        position_policy=None,
    )

    with pytest.raises(StrategyValidationError) as raised:
        validate_strategy_version(strategy)

    assert [issue.path for issue in raised.value.issues] == [
        "conditions[0].operandA.window",
        "lag",
        "exit.holdingSignalSessions",
    ]


def test_requires_combinators_after_the_first_condition() -> None:
    request = CreateBacktestRequestTransport.model_validate(
        {
            "strategyVersion": {
                **strategy_with(
                    {
                        "operator": "GT",
                        "operandA": {"asset": "QQQ", "metric": "SIMPLE"},
                        "operandB": 0.0,
                    }
                ),
                "conditions": [
                    {
                        "operator": "GT",
                        "operandA": {"asset": "QQQ", "metric": "SIMPLE"},
                        "operandB": 0.0,
                    },
                    {
                        "operator": "LT",
                        "operandA": {"asset": "SPY", "metric": "SIMPLE"},
                        "operandB": 20.0,
                    },
                ],
            },
            "feeModel": {"commission": 0.0, "slippage": 0.0},
            "period": {"start": "2024-01-01", "end": "2024-12-31"},
        }
    ).to_domain()

    with pytest.raises(StrategyValidationError) as raised:
        validate_strategy_version(request.strategy_version)

    assert raised.value.issues[0].path == "conditions[1].logicalCombinator"


def test_rejects_mixed_condition_group_combinators() -> None:
    strategy = StrategyVersion(
        primary_signal_asset=AssetSymbol.QQQ,
        conditions=(
            Condition(
                ComparisonOperator.GT,
                None,
                MetricReference(AssetSymbol.QQQ, MetricType.SIMPLE, None),
                Decimal("0"),
            ),
            Condition(
                ComparisonOperator.LT,
                LogicalCombinator.AND,
                MetricReference(AssetSymbol.SPY, MetricType.SIMPLE, None),
                Decimal("20"),
            ),
            Condition(
                ComparisonOperator.GT,
                LogicalCombinator.OR,
                MetricReference(AssetSymbol.QQQ, MetricType.SIMPLE, None),
                Decimal("0"),
            ),
        ),
        execution_asset=AssetSymbol.TQQQ,
        lag=0,
        holding_signal_sessions=1,
        position_policy=None,
    )

    with pytest.raises(StrategyValidationError) as raised:
        validate_strategy_version(strategy)

    assert raised.value.issues == (
        ValidationIssue(
            "conditions[2].logicalCombinator",
            "MVP Condition Groups cannot mix AND and OR combinators.",
        ),
    )


@pytest.mark.parametrize(
    ("metric_type", "window"),
    [("RETURN", None), ("CHANGE", None)],
)
def test_requires_windows_for_return_and_change_metrics(
    metric_type: str, window: int | None
) -> None:
    metric: dict[str, object] = {"asset": "QQQ", "metric": metric_type}
    if window is not None:
        metric["window"] = window
    request = CreateBacktestRequestTransport.model_validate(
        {
            "strategyVersion": strategy_with(
                {"operator": "GT", "operandA": metric, "operandB": 0.0}
            ),
            "feeModel": {"commission": 0.0, "slippage": 0.0},
            "period": {"start": "2024-01-01", "end": "2024-12-31"},
        }
    ).to_domain()

    with pytest.raises(StrategyValidationError) as raised:
        validate_strategy_version(request.strategy_version)

    assert raised.value.issues[0].path == "conditions[0].operandA.window"


@pytest.mark.parametrize(
    "policy", [PositionPolicy(False, True, None), PositionPolicy(True, False, None)]
)
def test_rejects_unsupported_position_policy_values(policy: PositionPolicy) -> None:
    strategy = StrategyVersion(
        primary_signal_asset=AssetSymbol.QQQ,
        conditions=(
            Condition(
                operator=ComparisonOperator.GT,
                logical_combinator=None,
                operand_a=MetricReference(AssetSymbol.QQQ, MetricType.SIMPLE, None),
                operand_b=0.0,
            ),
        ),
        execution_asset=AssetSymbol.TQQQ,
        lag=0,
        holding_signal_sessions=1,
        position_policy=policy,
    )

    with pytest.raises(StrategyValidationError) as raised:
        validate_strategy_version(strategy)

    assert raised.value.issues[0].path in {
        "positionPolicy.longOnly",
        "positionPolicy.singlePosition",
    }
