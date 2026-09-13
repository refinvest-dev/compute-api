"""Canonical polling payload mapping for completed Backtest results."""

from datetime import date, datetime
from decimal import Decimal

from refinvest.engine.benchmark import BenchmarkReferences, BuyAndHoldReference
from refinvest.engine.results import AssembledBacktestResult
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.trades import Trade
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.domain import calendar_for_asset
from refinvest.marketdata.ports import SessionCalendarPort


def backtest_result_payload(
    run_id: str,
    assembled: AssembledBacktestResult,
    calendar: SessionCalendarPort,
    primary_signal_asset: AssetSymbol,
    execution_asset: AssetSymbol,
) -> dict[str, object]:
    """Encode an Engine result using the canonical durable polling representation."""
    metrics = assembled.performance.metrics
    trades = tuple(assembled.trades)
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
            for trade in trades
        ],
        "benchmark": _benchmark_payload(assembled.benchmark),
        "signalExecutionMarketRelation": _signal_execution_market_relation(
            primary_signal_asset, execution_asset
        ),
        "signalExecutionDelay": _signal_execution_delay_payload(
            trades, calendar, primary_signal_asset, execution_asset
        ),
        "sampleSizeWarning": assembled.sample_size_warning.value,
        "dataIntegrityStatus": {
            "datasetSnapshotId": assembled.data_integrity_status.dataset_snapshot_id,
            "corporateActionsApplied": assembled.data_integrity_status.corporate_actions_applied,
            "pointInTimeValidationPassed": (
                assembled.data_integrity_status.point_in_time_validation_passed
            ),
        },
    }


def _signal_execution_delay_payload(
    trades: tuple[Trade, ...],
    calendar: SessionCalendarPort,
    primary_signal_asset: AssetSymbol,
    execution_asset: AssetSymbol,
) -> dict[str, object]:
    """Return deterministic, calendar-based signal-to-entry delays in hours."""
    distribution = tuple(
        _signal_execution_delay_hours(trade, calendar, primary_signal_asset, execution_asset)
        for trade in trades
    )
    if not distribution:
        return {"median": "0", "max": "0", "distribution": []}

    ordered = sorted(distribution)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else _normalize_delay_hours((ordered[middle - 1] + ordered[middle]) / Decimal(2))
    )
    return {
        "median": str(median),
        "max": str(max(distribution)),
        "distribution": [str(delay) for delay in distribution],
    }


def _signal_execution_delay_hours(
    trade: Trade,
    calendar: SessionCalendarPort,
    primary_signal_asset: AssetSymbol,
    execution_asset: AssetSymbol,
) -> Decimal:
    signal_time = _completion_at(calendar, primary_signal_asset, trade.signal_session)
    entry_time = _completion_at(calendar, execution_asset, trade.entry_session)
    seconds = Decimal(str((entry_time - signal_time).total_seconds()))
    if seconds < 0:
        raise TemporalResolutionError("Entry execution must not precede its signal completion.")
    return _normalize_delay_hours(seconds / Decimal(3600))


def _normalize_delay_hours(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001")).normalize()


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
        "asset": item.asset.value,
        "equityCurve": [
            {"date": point.session_date.isoformat(), "value": str(point.value)}
            for point in item.equity_curve
        ],
        "totalReturn": str(item.total_return),
        "cagr": _decimal_text(item.cagr),
        "mdd": str(item.mdd),
    }


def _signal_execution_market_relation(
    primary_signal_asset: AssetSymbol, execution_asset: AssetSymbol
) -> str:
    """Describe calendar relation without affecting temporal execution semantics."""
    if calendar_for_asset(primary_signal_asset.value) == calendar_for_asset(execution_asset.value):
        return "SAME_MARKET"
    return "CROSS_MARKET"


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
