"""Assemble immutable Engine calculation outputs without HTTP or persistence dependencies."""

from dataclasses import dataclass

from refinvest.engine.benchmark import BenchmarkReferences
from refinvest.engine.performance import PerformanceReport
from refinvest.engine.trades import SampleSizeWarning, Trade, sample_size_warning


@dataclass(frozen=True)
class ResultDataIntegrityStatus:
    """Snapshot provenance supplied by the application boundary."""

    dataset_snapshot_id: str
    corporate_actions_applied: bool
    point_in_time_validation_passed: bool


@dataclass(frozen=True)
class AssembledBacktestResult:
    """Pure Engine result before run identity and OpenAPI transport conversion."""

    performance: PerformanceReport
    trades: tuple[Trade, ...]
    benchmark: BenchmarkReferences
    sample_size_warning: SampleSizeWarning
    data_integrity_status: ResultDataIntegrityStatus


def assemble_backtest_result(
    performance: PerformanceReport,
    trades: tuple[Trade, ...],
    benchmark: BenchmarkReferences,
    data_integrity_status: ResultDataIntegrityStatus,
) -> AssembledBacktestResult:
    """Validate and compose deterministic result components from one pinned execution."""
    _validate_data_integrity_status(data_integrity_status)
    _validate_trade_count(performance, trades)
    _validate_zero_trade_metrics(performance, trades)
    return AssembledBacktestResult(
        performance=performance,
        trades=trades,
        benchmark=benchmark,
        sample_size_warning=sample_size_warning(trades),
        data_integrity_status=data_integrity_status,
    )


def _validate_data_integrity_status(status: ResultDataIntegrityStatus) -> None:
    if not status.dataset_snapshot_id.strip():
        raise ValueError("data_integrity_status.dataset_snapshot_id must not be empty.")


def _validate_trade_count(performance: PerformanceReport, trades: tuple[Trade, ...]) -> None:
    if performance.metrics.trade_count != len(trades):
        raise ValueError("Performance trade_count must match the realized trade count.")


def _validate_zero_trade_metrics(performance: PerformanceReport, trades: tuple[Trade, ...]) -> None:
    if trades:
        return
    metrics = performance.metrics
    undefined_metrics = (
        metrics.total_return,
        metrics.cagr,
        metrics.mdd,
        metrics.sharpe,
        metrics.win_rate,
        metrics.avg_trade_return,
        metrics.avg_holding_period,
        metrics.profit_factor,
    )
    if any(value is not None for value in undefined_metrics):
        raise ValueError("Zero-trade performance metrics must remain absent.")
