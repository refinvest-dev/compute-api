"""Daily Bar metric values evaluated from immutable snapshot data."""

from datetime import date
from decimal import Decimal

from refinvest.engine.temporal import resolve_metric_anchor_session
from refinvest.engine.validation.models import AssetSymbol, MetricReference, MetricType
from refinvest.marketdata.ports import MarketDataPort, SessionCalendarPort, require_daily_bar


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
