"""Semantic invariants for the MVP StrategyVersion payload."""

from dataclasses import dataclass

from refinvest.engine.validation.models import (
    AssetSymbol,
    MetricReference,
    MetricType,
    PositionPolicy,
    StrategyVersion,
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str


class StrategyValidationError(ValueError):
    """Raised when one or more StrategyVersion semantic invariants are broken."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("StrategyVersion validation failed.")


def validate_strategy_version(strategy: StrategyVersion) -> None:
    """Validate only documented DSL/MVP invariants, without data access."""
    issues: list[ValidationIssue] = []

    if not strategy.conditions:
        issues.append(ValidationIssue("conditions", "At least one condition is required."))

    for index, condition in enumerate(strategy.conditions):
        combinator_path = f"conditions[{index}].logicalCombinator"
        if index == 0 and condition.logical_combinator is not None:
            issues.append(
                ValidationIssue(combinator_path, "The first condition must not have a combinator.")
            )
        if index > 0 and condition.logical_combinator is None:
            issues.append(
                ValidationIssue(combinator_path, "Subsequent conditions require a combinator.")
            )

        _validate_metric(condition.operand_a, f"conditions[{index}].operandA", issues)
        if isinstance(condition.operand_b, MetricReference):
            _validate_metric(condition.operand_b, f"conditions[{index}].operandB", issues)

    if strategy.execution_asset is AssetSymbol.VIX:
        issues.append(ValidationIssue("executionAsset", "VIX cannot be an execution asset."))
    if strategy.lag < 0:
        issues.append(ValidationIssue("lag", "Lag must be zero or greater."))
    if strategy.holding_signal_sessions <= 0:
        issues.append(
            ValidationIssue(
                "exit.holdingSignalSessions", "Holding signal sessions must be greater than zero."
            )
        )

    _validate_position_policy(strategy.position_policy, issues)

    if issues:
        raise StrategyValidationError(tuple(issues))


def _validate_metric(
    metric: MetricReference,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if metric.metric is MetricType.SIMPLE and metric.window is not None:
        issues.append(ValidationIssue(f"{path}.window", "SIMPLE metrics must not define a window."))
    if metric.metric in {MetricType.RETURN, MetricType.CHANGE} and (
        metric.window is None or metric.window <= 0
    ):
        issues.append(
            ValidationIssue(
                f"{path}.window",
                "RETURN and CHANGE metrics require a window greater than zero.",
            )
        )


def _validate_position_policy(
    policy: PositionPolicy | None,
    issues: list[ValidationIssue],
) -> None:
    if policy is None:
        return
    if policy.long_only is not None and not policy.long_only:
        issues.append(
            ValidationIssue("positionPolicy.longOnly", "MVP only supports long-only positions.")
        )
    if policy.single_position is not None and not policy.single_position:
        issues.append(
            ValidationIssue("positionPolicy.singlePosition", "MVP only supports a single position.")
        )
