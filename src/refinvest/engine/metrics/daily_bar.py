"""Daily Bar metric values evaluated from immutable snapshot data."""

from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from refinvest.engine.temporal import (
    resolve_metric_anchor_session,
    resolve_metric_lookback_session,
)
from refinvest.engine.validation.models import AssetSymbol, MetricReference, MetricType
from refinvest.marketdata.ports import (
    DatasetCorruptionError,
    MarketDataPort,
    SessionCalendarPort,
    require_daily_bar,
)

METRIC_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


def evaluate_metric(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    primary_signal_asset: AssetSymbol,
    signal_session: date,
    metric: MetricReference,
) -> Decimal:
    """Evaluate one supported metric reference from immutable snapshot data."""
    if metric.metric is MetricType.SIMPLE:
        return evaluate_simple_metric(
            calendar, market_data, primary_signal_asset, signal_session, metric
        )
    if metric.metric in {MetricType.RETURN, MetricType.CHANGE}:
        return evaluate_lookback_metric(
            calendar, market_data, primary_signal_asset, signal_session, metric
        )
    raise ValueError(f"Unsupported metric type: {metric.metric}")


def evaluate_simple_metric(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    primary_signal_asset: AssetSymbol,
    signal_session: date,
    metric: MetricReference,
) -> Decimal:
    """Return the selected completed Daily Bar close for one SIMPLE metric reference."""
    if metric.metric is not MetricType.SIMPLE or metric.window is not None:
        raise ValueError("evaluate_simple_metric requires a SIMPLE metric without a window.")
    anchor_session = resolve_metric_anchor_session(
        calendar,
        primary_signal_asset.value,
        signal_session,
        metric.asset.value,
    )
    return require_daily_bar(market_data.get_daily_bar(metric.asset.value, anchor_session)).close


def evaluate_lookback_metric(
    calendar: SessionCalendarPort,
    market_data: MarketDataPort,
    primary_signal_asset: AssetSymbol,
    signal_session: date,
    metric: MetricReference,
) -> Decimal:
    """Return one RETURN or CHANGE value from exact referenced-asset sessions."""
    if metric.metric not in {MetricType.RETURN, MetricType.CHANGE} or metric.window is None:
        raise ValueError(
            "evaluate_lookback_metric requires a RETURN or CHANGE metric with a window."
        )
    if metric.window < 1:
        raise ValueError("Metric window must be positive.")
    anchor_session = resolve_metric_anchor_session(
        calendar,
        primary_signal_asset.value,
        signal_session,
        metric.asset.value,
    )
    lookback_session = resolve_metric_lookback_session(
        calendar, metric.asset.value, anchor_session, metric.window
    )
    anchor_close = require_daily_bar(
        market_data.get_daily_bar(metric.asset.value, anchor_session)
    ).close
    lookback_close = require_daily_bar(
        market_data.get_daily_bar(metric.asset.value, lookback_session)
    ).close
    if metric.metric is MetricType.CHANGE:
        return anchor_close - lookback_close
    if lookback_close <= Decimal(0):
        raise DatasetCorruptionError("A RETURN metric requires a positive lookback close.")
    with localcontext(METRIC_DECIMAL_CONTEXT):
        return (anchor_close / lookback_close) - Decimal(1)
