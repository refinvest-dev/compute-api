"""OpenAPI transport models for future Backtest submission routes."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from refinvest.engine.validation import models as domain


class AssetSymbolTransport(StrEnum):
    QQQ = "QQQ"
    SPY = "SPY"
    TQQQ = "TQQQ"
    SOXL = "SOXL"
    BTCUSDT = "BTCUSDT"
    VIX = "VIX"


class ExecutionAssetSymbolTransport(StrEnum):
    QQQ = "QQQ"
    SPY = "SPY"
    TQQQ = "TQQQ"
    SOXL = "SOXL"
    BTCUSDT = "BTCUSDT"


class MetricTypeTransport(StrEnum):
    SIMPLE = "SIMPLE"
    RETURN = "RETURN"
    CHANGE = "CHANGE"


class ComparisonOperatorTransport(StrEnum):
    LT = "LT"
    GT = "GT"
    LTE = "LTE"
    GTE = "GTE"


class LogicalCombinatorTransport(StrEnum):
    AND = "AND"
    OR = "OR"


class DuplicateEntryTransport(StrEnum):
    IGNORE = "IGNORE"


class FatalErrorCodeTransport(StrEnum):
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"
    DATASET_CORRUPTION = "DATASET_CORRUPTION"
    CALENDAR_RESOLUTION_FAILED = "CALENDAR_RESOLUTION_FAILED"
    PRICE_DATA_MISSING = "PRICE_DATA_MISSING"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    DSL_INVALID = "DSL_INVALID"


class MetricReferenceTransport(BaseModel):
    asset: AssetSymbolTransport
    metric: MetricTypeTransport
    window: int | None = Field(default=None, ge=1)

    def to_domain(self) -> domain.MetricReference:
        return domain.MetricReference(
            asset=domain.AssetSymbol(self.asset.value),
            metric=domain.MetricType(self.metric.value),
            window=self.window,
        )


class ConditionTransport(BaseModel):
    operator: ComparisonOperatorTransport
    logical_combinator: LogicalCombinatorTransport | None = Field(
        default=None,
        alias="logicalCombinator",
    )
    operand_a: MetricReferenceTransport = Field(alias="operandA")
    operand_b: MetricReferenceTransport | Decimal = Field(alias="operandB")

    def to_domain(self) -> domain.Condition:
        operand_b: domain.MetricOperand
        if isinstance(self.operand_b, MetricReferenceTransport):
            operand_b = self.operand_b.to_domain()
        else:
            operand_b = self.operand_b
        return domain.Condition(
            operator=domain.ComparisonOperator(self.operator.value),
            logical_combinator=(
                domain.LogicalCombinator(self.logical_combinator.value)
                if self.logical_combinator is not None
                else None
            ),
            operand_a=self.operand_a.to_domain(),
            operand_b=operand_b,
        )


class PositionPolicyTransport(BaseModel):
    long_only: bool | None = Field(default=None, alias="longOnly")
    single_position: bool | None = Field(default=None, alias="singlePosition")
    duplicate_entry: DuplicateEntryTransport | None = Field(default=None, alias="duplicateEntry")

    def to_domain(self) -> domain.PositionPolicy:
        return domain.PositionPolicy(
            long_only=self.long_only,
            single_position=self.single_position,
            duplicate_entry=(
                domain.DuplicateEntryPolicy(self.duplicate_entry.value)
                if self.duplicate_entry is not None
                else None
            ),
        )


class ExitTransport(BaseModel):
    holding_signal_sessions: int = Field(alias="holdingSignalSessions", ge=1)


class StrategyVersionTransport(BaseModel):
    primary_signal_asset: AssetSymbolTransport = Field(alias="primarySignalAsset")
    conditions: list[ConditionTransport] = Field(min_length=1)
    execution_asset: ExecutionAssetSymbolTransport = Field(alias="executionAsset")
    lag: int = Field(ge=0)
    exit: ExitTransport
    position_policy: PositionPolicyTransport | None = Field(default=None, alias="positionPolicy")

    def to_domain(self) -> domain.StrategyVersion:
        return domain.StrategyVersion(
            primary_signal_asset=domain.AssetSymbol(self.primary_signal_asset.value),
            conditions=tuple(condition.to_domain() for condition in self.conditions),
            execution_asset=domain.AssetSymbol(self.execution_asset.value),
            lag=self.lag,
            holding_signal_sessions=self.exit.holding_signal_sessions,
            position_policy=(
                self.position_policy.to_domain() if self.position_policy is not None else None
            ),
        )


class FeeModelTransport(BaseModel):
    commission: Decimal
    slippage: Decimal

    def to_domain(self) -> domain.FeeModel:
        return domain.FeeModel(commission=self.commission, slippage=self.slippage)


class PeriodTransport(BaseModel):
    start: date
    end: date

    def to_domain(self) -> domain.Period:
        return domain.Period(start=self.start, end=self.end)


class CreateBacktestRequestTransport(BaseModel):
    strategy_version_id: str | None = Field(default=None, alias="strategyVersionId")
    strategy_version: StrategyVersionTransport = Field(alias="strategyVersion")
    fee_model: FeeModelTransport = Field(alias="feeModel")
    period: PeriodTransport
    dataset_snapshot_id: str | None = Field(default=None, alias="datasetSnapshotId")

    def to_domain(self) -> domain.BacktestRequest:
        return domain.BacktestRequest(
            strategy_version_id=self.strategy_version_id,
            strategy_version=self.strategy_version.to_domain(),
            fee_model=self.fee_model.to_domain(),
            period=self.period.to_domain(),
            dataset_snapshot_id=self.dataset_snapshot_id,
        )


class BacktestAcceptedTransport(BaseModel):
    run_id: str = Field(alias="runId")
    strategy_version_id: str | None = Field(default=None, alias="strategyVersionId")
    status: Literal["PENDING"]


class BacktestStatusTransport(BaseModel):
    run_id: str = Field(alias="runId")
    strategy_version_id: str | None = Field(default=None, alias="strategyVersionId")
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    dataset_snapshot_id: str | None = Field(default=None, alias="datasetSnapshotId")
    actual_period: PeriodTransport | None = Field(default=None, alias="actualPeriod")
    engine_version: str | None = Field(default=None, alias="engineVersion")
    result: dict[str, object] | None = None
    failure_reason: str | None = Field(default=None, alias="failureReason")
    error_code: FatalErrorCodeTransport | None = Field(default=None, alias="errorCode")
