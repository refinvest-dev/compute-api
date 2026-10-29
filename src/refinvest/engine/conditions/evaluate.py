"""Evaluate validated DSL conditions against one immutable DatasetSnapshot."""

from datetime import date
from decimal import Decimal

from refinvest.engine.metrics import evaluate_metric
from refinvest.engine.validation.models import (
    AssetSymbol,
    ComparisonOperator,
    Condition,
    LogicalCombinator,
    MetricOperand,
    MetricReference,
)
from refinvest.marketdata.ports import MarketDataPort, SessionCalendarPort


def evaluate_condition_group(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    primary_signal_asset: AssetSymbol,
    signal_session: date,
    conditions: tuple[Condition, ...],
) -> bool:
    """Evaluate every condition before combining one validated MVP group."""
    combinator = _group_combinator(conditions)
    results = tuple(
        _evaluate_condition(calendar, market_data, primary_signal_asset, signal_session, condition)
        for condition in conditions
    )
    if combinator is None:
        return results[0]
    if combinator is LogicalCombinator.AND:
        return all(results)
    return any(results)


def _group_combinator(conditions: tuple[Condition, ...]) -> LogicalCombinator | None:
    if not conditions:
        raise ValueError("A Condition Group requires at least one condition.")
    if conditions[0].logical_combinator is not None:
        raise ValueError("The first condition must not have a combinator.")
    if len(conditions) == 1:
        return None

    combinator = conditions[1].logical_combinator
    if combinator is None:
        raise ValueError("Subsequent conditions require a combinator.")
    if any(condition.logical_combinator is not combinator for condition in conditions[1:]):
        raise ValueError("MVP Condition Groups cannot mix AND and OR combinators.")
    return combinator


def _evaluate_condition(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    primary_signal_asset: AssetSymbol,
    signal_session: date,
    condition: Condition,
) -> bool:
    operand_a = evaluate_metric(
        calendar,
        market_data,
        primary_signal_asset,
        signal_session,
        condition.operand_a,
    )
    operand_b = _evaluate_operand(
        calendar,
        market_data,
        primary_signal_asset,
        signal_session,
        condition.operand_b,
    )
    if condition.operator is ComparisonOperator.LT:
        return operand_a < operand_b
    if condition.operator is ComparisonOperator.GT:
        return operand_a > operand_b
    if condition.operator is ComparisonOperator.LTE:
        return operand_a <= operand_b
    if condition.operator is ComparisonOperator.GTE:
        return operand_a >= operand_b
    raise ValueError(f"Unsupported comparison operator: {condition.operator}")


def _evaluate_operand(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    primary_signal_asset: AssetSymbol,
    signal_session: date,
    operand: MetricOperand,
) -> Decimal:
    if isinstance(operand, MetricReference):
        return evaluate_metric(calendar, market_data, primary_signal_asset, signal_session, operand)
    return operand
