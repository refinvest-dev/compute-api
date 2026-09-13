"""Snapshot-pinned adapter that runs the Daily Bar backtest pipeline."""

from datetime import date

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
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader
from refinvest.engine.benchmark import evaluate_benchmark_references
from refinvest.engine.entries import resolve_entry_candidates
from refinvest.engine.execution import (
    price_entry_execution_schedules,
    price_exit_execution_schedules,
)
from refinvest.engine.performance import evaluate_performance
from refinvest.engine.position import admit_positions
from refinvest.engine.results import ResultDataIntegrityStatus, assemble_backtest_result
from refinvest.engine.signals import evaluate_signals
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.trades import realize_trades
from refinvest.engine.validation import StrategyValidationError, validate_strategy_version
from refinvest.engine.validation.models import (
    AssetSymbol,
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
            request = backtest_request_from_job_payload(job.claim.request_payload)
            validate_strategy_version(request.strategy_version)
            data = self._data_loader.load(job.dataset_snapshot)
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
                result_payload=backtest_result_payload(
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
