"""Snapshot-pinned adapter that runs the Daily Bar backtest pipeline."""

from collections.abc import Callable
from datetime import date
from typing import Literal, Protocol, TypeVar

from refinvest.application.backtest_job_payload import (
    BacktestJobPayloadError,
    backtest_request_from_job_payload,
)
from refinvest.application.backtest_result_payload import backtest_result_payload
from refinvest.application.backtest_worker_host import (
    CompletedBacktestExecution,
    ExecutableBacktestJob,
    FatalErrorCode,
    TerminalBacktestFailure,
)
from refinvest.application.snapshot_execution_data import (
    SnapshotExecutionData,
    SnapshotExecutionDataLoader,
)
from refinvest.engine.benchmark import evaluate_benchmark_references
from refinvest.engine.entries import resolve_entry_candidates
from refinvest.engine.execution import (
    EntryExecution,
    ExitExecution,
    price_entry_execution_schedules,
    price_exit_execution_schedules,
)
from refinvest.engine.performance import evaluate_performance
from refinvest.engine.position import PlannedPosition, admit_positions
from refinvest.engine.results import (
    AssembledBacktestResult,
    ResultDataIntegrityStatus,
    assemble_backtest_result,
)
from refinvest.engine.signals import Signal, evaluate_signals
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.trades import Trade, realize_trades
from refinvest.engine.validation import StrategyValidationError, validate_strategy_version
from refinvest.engine.validation.models import (
    AssetSymbol,
    BacktestRequest,
    MetricReference,
    Period,
    StrategyVersion,
)
from refinvest.marketdata.errors import CalendarResolutionError
from refinvest.marketdata.ports import (
    DatasetCorruptionError,
    MissingRequiredMarketDataError,
    SessionCalendarPort,
)

ExecutionStage = Literal[
    "request_strategy_validation",
    "snapshot_load",
    "calendar_resolution",
    "signal_evaluation",
    "entry_exit_resolution",
    "trade_realization",
    "performance_benchmark_assembly",
    "result_serialization",
]


class ExecutionStageObserver(Protocol):
    """Optional application-boundary observer; implementations must not affect results."""

    def stage_started(self, stage: ExecutionStage) -> None: ...

    def stage_completed(self, stage: ExecutionStage) -> None: ...


_T = TypeVar("_T")


class DailyBarBacktestExecutor:
    """Concrete worker executor; it never consults latest data or a vendor API."""

    def __init__(self, data_loader: SnapshotExecutionDataLoader, engine_version: str) -> None:
        if not engine_version.strip():
            raise ValueError("engine_version must not be empty.")
        self._data_loader = data_loader
        self._engine_version = engine_version

    def execute(
        self,
        job: ExecutableBacktestJob,
        observer: ExecutionStageObserver | None = None,
    ) -> CompletedBacktestExecution | TerminalBacktestFailure:
        try:
            request = _observe(
                observer,
                "request_strategy_validation",
                lambda: _validated_request(job.claim.request_payload),
            )
            return self.execute_request(job, request, observer)
        except _TerminalExecutionError as error:
            return TerminalBacktestFailure(str(error), error.error_code)
        except BacktestJobPayloadError as error:
            return TerminalBacktestFailure(str(error), "DATASET_CORRUPTION")
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

    def execute_request(
        self,
        job: ExecutableBacktestJob,
        request: BacktestRequest,
        observer: ExecutionStageObserver | None = None,
    ) -> CompletedBacktestExecution | TerminalBacktestFailure:
        """Run the production pipeline for an already transport-validated request."""
        try:
            data = _observe(
                observer, "snapshot_load", lambda: self._data_loader.load(job.dataset_snapshot)
            )
            if data.snapshot_id != job.dataset_snapshot.snapshot_id:
                raise _TerminalExecutionError(
                    "Loaded market data does not belong to the pinned DatasetSnapshot.",
                    "DATASET_CORRUPTION",
                )
            actual_period, signal_sessions = _observe(
                observer,
                "calendar_resolution",
                lambda: _resolve_calendar_inputs(data.calendar, request),
            )
            strategy = request.strategy_version
            signals = _observe(
                observer,
                "signal_evaluation",
                lambda: evaluate_signals(
                    data.calendar, data.market_data, strategy, signal_sessions
                ),
            )
            positions, entries, exits = _observe(
                observer,
                "entry_exit_resolution",
                lambda: _resolve_entries_and_exits(data, request, signals),
            )
            trades = _observe(
                observer,
                "trade_realization",
                lambda: realize_trades(
                    request.fee_model,
                    positions,
                    entries,
                    exits,
                    strategy.holding_signal_sessions,
                ),
            )
            result = _observe(
                observer,
                "performance_benchmark_assembly",
                lambda: _assemble_result(job, data, request, actual_period, trades),
            )
            return CompletedBacktestExecution(
                result_payload=_observe(
                    observer,
                    "result_serialization",
                    lambda: backtest_result_payload(
                        job.claim.run_id,
                        result,
                        data.calendar,
                        strategy.primary_signal_asset,
                        strategy.execution_asset,
                    ),
                ),
                actual_period=actual_period,
                engine_version=self._engine_version,
            )
        except _TerminalExecutionError as error:
            return TerminalBacktestFailure(str(error), error.error_code)
        except BacktestJobPayloadError as error:
            return TerminalBacktestFailure(str(error), "DATASET_CORRUPTION")
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


def _validated_request(payload: dict[str, object]) -> BacktestRequest:
    request = backtest_request_from_job_payload(payload)
    validate_strategy_version(request.strategy_version)
    return request


def _resolve_calendar_inputs(
    calendar: SessionCalendarPort, request: BacktestRequest
) -> tuple[Period, tuple[date, ...]]:
    actual_period = resolve_actual_period(
        calendar, request.period, _strategy_assets(request.strategy_version)
    )
    sessions = _sessions_in_period(
        calendar, request.strategy_version.primary_signal_asset, actual_period
    )
    return actual_period, sessions


def _resolve_entries_and_exits(
    data: SnapshotExecutionData,
    request: BacktestRequest,
    signals: tuple[Signal, ...],
) -> tuple[tuple[PlannedPosition, ...], tuple[EntryExecution, ...], tuple[ExitExecution, ...]]:
    strategy = request.strategy_version
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
    return positions, entries, exits


def _assemble_result(
    job: ExecutableBacktestJob,
    data: SnapshotExecutionData,
    request: BacktestRequest,
    actual_period: Period,
    trades: tuple[Trade, ...],
) -> AssembledBacktestResult:
    strategy = request.strategy_version
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
    return assemble_backtest_result(
        performance,
        trades,
        benchmarks,
        ResultDataIntegrityStatus(
            job.dataset_snapshot.snapshot_id,
            data.corporate_actions_applied,
            data.point_in_time_validation_passed,
        ),
    )


def _observe(
    observer: ExecutionStageObserver | None,
    stage: ExecutionStage,
    operation: Callable[[], _T],
) -> _T:
    if observer is None:
        return operation()
    try:
        observer.stage_started(stage)
    except Exception:
        return operation()
    try:
        return operation()
    finally:
        try:
            observer.stage_completed(stage)
        except Exception:
            pass


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
