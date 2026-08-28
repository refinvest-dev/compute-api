"""Immutable domain values used by backtest request validation."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class AssetSymbol(StrEnum):
    QQQ = "QQQ"
    SPY = "SPY"
    TQQQ = "TQQQ"
    SOXL = "SOXL"
    BTCUSDT = "BTCUSDT"
    VIX = "VIX"


class MetricType(StrEnum):
    SIMPLE = "SIMPLE"
    RETURN = "RETURN"
    CHANGE = "CHANGE"


class ComparisonOperator(StrEnum):
    LT = "LT"
    GT = "GT"
    LTE = "LTE"
    GTE = "GTE"


class LogicalCombinator(StrEnum):
    AND = "AND"
    OR = "OR"


class DuplicateEntryPolicy(StrEnum):
    IGNORE = "IGNORE"


@dataclass(frozen=True)
class MetricReference:
    asset: AssetSymbol
    metric: MetricType
    window: int | None


MetricOperand = MetricReference | Decimal


@dataclass(frozen=True)
class Condition:
    operator: ComparisonOperator
    logical_combinator: LogicalCombinator | None
    operand_a: MetricReference
    operand_b: MetricOperand


@dataclass(frozen=True)
class PositionPolicy:
    long_only: bool | None
    single_position: bool | None
    duplicate_entry: DuplicateEntryPolicy | None


@dataclass(frozen=True)
class StrategyVersion:
    primary_signal_asset: AssetSymbol
    conditions: tuple[Condition, ...]
    execution_asset: AssetSymbol
    lag: int
    holding_signal_sessions: int
    position_policy: PositionPolicy | None


@dataclass(frozen=True)
class FeeModel:
    commission: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class Period:
    start: date
    end: date


@dataclass(frozen=True)
class BacktestRequest:
    strategy_version_id: str | None
    strategy_version: StrategyVersion
    fee_model: FeeModel
    period: Period
    dataset_snapshot_id: str | None
