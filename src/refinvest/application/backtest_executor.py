"""Snapshot-pinned adapter that runs the Daily Bar backtest pipeline."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, cast

from refinvest.application.backtest_worker_host import (
    CompletedBacktestExecution,
    ExecutableBacktestJob,
    FatalErrorCode,
    TerminalBacktestFailure,
)
from refinvest.engine.benchmark import (
    BenchmarkReferences,
    BuyAndHoldReference,
    evaluate_benchmark_references,
)
from refinvest.engine.entries import resolve_entry_candidates
from refinvest.engine.execution import (
    price_entry_execution_schedules,
    price_exit_execution_schedules,
)
from refinvest.engine.performance import evaluate_performance
from refinvest.engine.position import admit_positions
from refinvest.engine.results import (
    AssembledBacktestResult,
    ResultDataIntegrityStatus,
    assemble_backtest_result,
)
from refinvest.engine.signals import evaluate_signals
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.trades import Trade, realize_trades
from refinvest.engine.validation import StrategyValidationError, validate_strategy_version
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
from refinvest.marketdata.adapters.in_memory import CalendarResolutionError
from refinvest.marketdata.ports import (
    DatasetCorruptionError,
    MarketDataPort,
    MissingRequiredMarketDataError,
    SessionCalendarPort,
)


@dataclass(frozen=True)
class SnapshotExecutionData:
    """Immutable Engine inputs plus ingestion-provided integrity facts."""

    snapshot_id: str
    calendar: SessionCalendarPort
    market_data: MarketDataPort
    corporate_actions_applied: bool
    point_in_time_validation_passed: bool


class SnapshotExecutionDataLoader(Protocol):
    """Loads only artifacts belonging to the worker's already-pinned snapshot."""

    def load(self, snapshot_id: str) -> SnapshotExecutionData: ...


class DailyBarBacktestExecutor:
    """Concrete worker executor; it never consults latest data or a vendor API."""

    def __init__(self, data_loader: SnapshotExecutionDataLoader, engine_version: str) -> None:
        if not engine_version.strip():
            raise ValueError("engine_version must not be empty.")
        self._data_loader = data_loader
        self._engine_version = engine_version

    def execute(
        self, job: ExecutableBacktestJob
    ) -> CompletedBacktestExecution | TerminalBacktestFailure:
        try:
            request = request_from_payload(job.claim.request_payload)
            validate_strategy_version(request.strategy_version)
            data = self._data_loader.load(job.dataset_snapshot.snapshot_id)
            if data.snapshot_id != job.dataset_snapshot.snapshot_id:
                raise _TerminalExecutionError(
                    "Loaded market data does not belong to the pinned DatasetSnapshot.",
                    "DATASET_CORRUPTION",
                )
            actual_period = resolve_actual_period(
                data.calendar, request.period, _strategy_assets(request.strategy_version)
            )
            strategy = request.strategy_version
            signal_sessions = _sessions_in_period(
                data.calendar, strategy.primary_signal_asset, actual_period
            )
            signals = evaluate_signals(data.calendar, data.market_data, strategy, signal_sessions)
            candidates = resolve_entry_candidates(
                data.calendar, strategy.primary_signal_asset, strategy.lag, signals
            )
            positions = admit_positions(
                data.calendar,
                strategy.execution_asset,
                strategy.holding_signal_sessions,
                candidates,
            )
            entries = price_entry_execution_schedules(
                data.market_data,
                request.fee_model.slippage,
                tuple(position.entry for position in positions),
            )
            exits = price_exit_execution_schedules(
                data.market_data,
                request.fee_model.slippage,
                tuple(position.exit for position in positions),
            )
            trades = realize_trades(
                request.fee_model,
                positions,
                entries,
                exits,
                strategy.holding_signal_sessions,
            )
            performance = evaluate_performance(
                data.calendar,
                data.market_data,
                strategy.execution_asset,
                request.fee_model,
                actual_period,
                trades,
            )
            benchmarks = evaluate_benchmark_references(
                data.calendar,
                data.market_data,
                strategy.execution_asset,
                strategy.primary_signal_asset,
                actual_period,
            )
            result = assemble_backtest_result(
                performance,
                trades,
                benchmarks,
                ResultDataIntegrityStatus(
                    job.dataset_snapshot.snapshot_id,
                    data.corporate_actions_applied,
                    data.point_in_time_validation_passed,
                ),
            )
            return CompletedBacktestExecution(
                result_payload=_result_payload(
                    job.claim.run_id,
                    result,
                    data.calendar,
                    strategy.primary_signal_asset,
                    strategy.execution_asset,
                ),
                actual_period=actual_period,
                engine_version=self._engine_version,
            )
        except _TerminalExecutionError as error:
            return TerminalBacktestFailure(str(error), error.error_code)
        except (CalendarResolutionError, TemporalResolutionError) as error:
            return TerminalBacktestFailure(str(error), "CALENDAR_RESOLUTION_FAILED")
        except MissingRequiredMarketDataError as error:
            return TerminalBacktestFailure(str(error), "MISSING_REQUIRED_DATA")
        except DatasetCorruptionError as error:
            return TerminalBacktestFailure(str(error), "DATASET_CORRUPTION")
        except StrategyValidationError as error:
            return TerminalBacktestFailure(str(error), "DATASET_CORRUPTION")
        except (TypeError, ValueError) as error:
            return TerminalBacktestFailure(str(error), "DATASET_CORRUPTION")


class _TerminalExecutionError(ValueError):
    def __init__(
        self,
        message: str,
        error_code: FatalErrorCode,
    ) -> None:
        self.error_code: FatalErrorCode = error_code
        super().__init__(message)


def resolve_actual_period(
    calendar: SessionCalendarPort, requested_period: Period, assets: frozenset[AssetSymbol]
) -> Period:
    """Intersect requested dates with every strategy asset's snapshot availability."""
    if requested_period.start > requested_period.end:
        raise _TerminalExecutionError(
            "Stored request period start must not exceed end.", "DATASET_CORRUPTION"
        )
    if not assets:
        raise _TerminalExecutionError("Stored strategy has no assets.", "DATASET_CORRUPTION")
    bounds = tuple(_availability_bounds(calendar, asset) for asset in assets)
    start = max(requested_period.start, *(first for first, _ in bounds))
    end = min(requested_period.end, *(last for _, last in bounds))
    if start > end:
        raise _TerminalExecutionError(
            "Requested period has no common DatasetSnapshot availability.",
            "CALENDAR_RESOLUTION_FAILED",
        )
    return Period(start, end)


def request_from_payload(payload: dict[str, object]) -> BacktestRequest:
    """Restore the canonical, Decimal-string job payload without transport dependencies."""
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
        raise _TerminalExecutionError(
            "Stored backtest request is invalid.", "DATASET_CORRUPTION"
        ) from error


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
    raise _TerminalExecutionError("Stored condition operandB is invalid.", "DATASET_CORRUPTION")


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


def _strategy_assets(strategy: StrategyVersion) -> frozenset[AssetSymbol]:
    assets = {strategy.primary_signal_asset, strategy.execution_asset}
    for condition in strategy.conditions:
        assets.add(condition.operand_a.asset)
        if isinstance(condition.operand_b, MetricReference):
            assets.add(condition.operand_b.asset)
    return frozenset(assets)


def _availability_bounds(calendar: SessionCalendarPort, asset: AssetSymbol) -> tuple[date, date]:
    sessions = calendar.expected_sessions(asset.value)
    if sessions != tuple(sorted(set(sessions))) or not sessions:
        raise TemporalResolutionError("Asset availability sessions must be ordered and non-empty.")
    return sessions[0], sessions[-1]


def _sessions_in_period(
    calendar: SessionCalendarPort, asset: AssetSymbol, period: Period
) -> tuple[date, ...]:
    sessions = tuple(
        session
        for session in calendar.expected_sessions(asset.value)
        if period.start <= session <= period.end
    )
    if not sessions:
        raise TemporalResolutionError("The actual period has no Primary Signal Asset session.")
    return sessions


def _result_payload(
    run_id: str,
    assembled: AssembledBacktestResult,
    calendar: SessionCalendarPort,
    primary_signal_asset: AssetSymbol,
    execution_asset: AssetSymbol,
) -> dict[str, object]:
    metrics = assembled.performance.metrics
    return {
        "backtestRunId": run_id,
        "metrics": {
            "totalReturn": _decimal_text(metrics.total_return),
            "cagr": _decimal_text(metrics.cagr),
            "mdd": _decimal_text(metrics.mdd),
            "sharpe": _decimal_text(metrics.sharpe),
            "winRate": _decimal_text(metrics.win_rate),
            "tradeCount": metrics.trade_count,
            "avgTradeReturn": _decimal_text(metrics.avg_trade_return),
            "avgHoldingPeriod": _decimal_text(metrics.avg_holding_period),
            "profitFactor": _decimal_text(metrics.profit_factor),
        },
        "equityCurve": [
            {"date": point.session_date.isoformat(), "value": str(point.value)}
            for point in assembled.performance.equity_curve
        ],
        "trades": [
            _trade_payload(trade, calendar, primary_signal_asset, execution_asset)
            for trade in assembled.trades
        ],
        "benchmark": _benchmark_payload(assembled.benchmark),
        "sampleSizeWarning": assembled.sample_size_warning.value,
        "dataIntegrityStatus": {
            "datasetSnapshotId": assembled.data_integrity_status.dataset_snapshot_id,
            "corporateActionsApplied": assembled.data_integrity_status.corporate_actions_applied,
            "pointInTimeValidationPassed": (
                assembled.data_integrity_status.point_in_time_validation_passed
            ),
        },
    }


def _trade_payload(
    item: Trade,
    calendar: SessionCalendarPort,
    primary_signal_asset: AssetSymbol,
    execution_asset: AssetSymbol,
) -> dict[str, object]:
    return {
        "signalTime": _completion_at(
            calendar, primary_signal_asset, item.signal_session
        ).isoformat(),
        "entryTime": _completion_at(calendar, execution_asset, item.entry_session).isoformat(),
        "entryPrice": str(item.entry_price),
        "exitTime": _completion_at(calendar, execution_asset, item.exit_session).isoformat(),
        "exitPrice": str(item.exit_price),
        "returnPct": str(item.return_pct),
        "holdingPeriod": item.holding_signal_sessions,
    }


def _completion_at(calendar: SessionCalendarPort, asset: AssetSymbol, session: date) -> datetime:
    matches = tuple(
        completion
        for completion in calendar.expected_session_completions(asset.value)
        if completion.asset_session.session_date == session
    )
    if len(matches) != 1:
        raise TemporalResolutionError(
            "Trade timestamp calendar completion is ambiguous or unavailable."
        )
    return matches[0].completed_at


def _benchmark_payload(references: BenchmarkReferences) -> dict[str, object]:
    payload: dict[str, object] = {"primary": _reference_payload(references.primary)}
    if references.secondary_reference is not None:
        payload["secondaryReference"] = _reference_payload(references.secondary_reference)
    return payload


def _reference_payload(item: BuyAndHoldReference) -> dict[str, object]:
    return {
        "totalReturn": str(item.total_return),
        "cagr": _decimal_text(item.cagr),
        "mdd": str(item.mdd),
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _TerminalExecutionError(f"Stored {field} must be an object.", "DATASET_CORRUPTION")
    return cast(dict[str, object], value)


def _list(payload: dict[str, object], field: str) -> list[object]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise _TerminalExecutionError(f"Stored {field} must be an array.", "DATASET_CORRUPTION")
    return cast(list[object], value)


def _string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise _TerminalExecutionError(f"Stored {field} must be a string.", "DATASET_CORRUPTION")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise _TerminalExecutionError(
        "Stored optional identifier must be a string.", "DATASET_CORRUPTION"
    )


def _integer(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _TerminalExecutionError(f"Stored {field} must be an integer.", "DATASET_CORRUPTION")
    return value


def _decimal(payload: dict[str, object], field: str) -> Decimal:
    value = _string(payload, field)
    try:
        return Decimal(value)
    except ValueError as error:
        raise _TerminalExecutionError(
            f"Stored {field} must be a Decimal string.", "DATASET_CORRUPTION"
        ) from error


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
