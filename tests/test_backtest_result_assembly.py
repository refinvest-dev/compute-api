from datetime import date
from decimal import Decimal

import pytest

from refinvest.engine.benchmark import (
    BenchmarkCurvePoint,
    BenchmarkReferences,
    BuyAndHoldReference,
)
from refinvest.engine.performance import EquityCurvePoint, PerformanceMetrics, PerformanceReport
from refinvest.engine.results import (
    ResultDataIntegrityStatus,
    assemble_backtest_result,
)
from refinvest.engine.trades import SampleSizeWarning, Trade
from refinvest.engine.validation.models import AssetSymbol


def trade() -> Trade:
    return Trade(
        signal_session=date(2025, 1, 2),
        entry_session=date(2025, 1, 3),
        entry_price=Decimal("100"),
        entry_cost=Decimal("100"),
        exit_session=date(2025, 1, 6),
        exit_price=Decimal("110"),
        exit_proceeds=Decimal("110"),
        return_pct=Decimal("0.1"),
        holding_signal_sessions=2,
    )


def performance(trade_count: int = 1) -> PerformanceReport:
    if trade_count == 0:
        metrics = PerformanceMetrics(None, None, None, None, None, 0, None, None, None)
    else:
        metrics = PerformanceMetrics(
            Decimal("0.1"),
            Decimal("0.1"),
            Decimal("0"),
            Decimal("1"),
            Decimal("1"),
            trade_count,
            Decimal("0.1"),
            Decimal("2"),
            None,
        )
    return PerformanceReport((EquityCurvePoint(date(2025, 1, 3), Decimal("1.1")),), metrics)


def benchmark() -> BenchmarkReferences:
    primary = BuyAndHoldReference(
        AssetSymbol.TQQQ,
        (BenchmarkCurvePoint(date(2025, 1, 3), Decimal("1")),),
        Decimal("0"),
        None,
        Decimal("0"),
    )
    return BenchmarkReferences(primary, None)


def data_integrity_status(snapshot_id: str = "snapshot-1") -> ResultDataIntegrityStatus:
    return ResultDataIntegrityStatus(snapshot_id, True, True)


def test_assembles_deterministic_result_and_derives_sample_warning() -> None:
    inputs = (performance(), (trade(),), benchmark(), data_integrity_status())

    result = assemble_backtest_result(*inputs)

    assert result.sample_size_warning is SampleSizeWarning.LOW
    assert result.performance is inputs[0]
    assert result.trades == inputs[1]
    assert result.benchmark is inputs[2]
    assert result.data_integrity_status is inputs[3]
    assert result == assemble_backtest_result(*inputs)


def test_zero_trade_result_preserves_absent_metrics() -> None:
    result = assemble_backtest_result(performance(0), (), benchmark(), data_integrity_status())

    assert result.sample_size_warning is SampleSizeWarning.ZERO
    assert result.performance.metrics.total_return is None


def test_rejects_mismatched_performance_trade_count() -> None:
    with pytest.raises(ValueError, match="trade_count must match"):
        assemble_backtest_result(performance(2), (trade(),), benchmark(), data_integrity_status())


def test_rejects_fabricated_zero_trade_metrics() -> None:
    fabricated_metrics = PerformanceMetrics(
        Decimal("0"), None, None, None, None, 0, None, None, None
    )
    fabricated_performance = PerformanceReport(
        (EquityCurvePoint(date(2025, 1, 3), Decimal("1")),), fabricated_metrics
    )

    with pytest.raises(ValueError, match="must remain absent"):
        assemble_backtest_result(fabricated_performance, (), benchmark(), data_integrity_status())


def test_rejects_empty_snapshot_provenance() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        assemble_backtest_result(performance(), (trade(),), benchmark(), data_integrity_status(" "))
