"""ADR-052 result-payload regression coverage."""

from datetime import date
from decimal import Decimal
from typing import cast

from refinvest.application.backtest_executor import _result_payload
from refinvest.engine.benchmark import BenchmarkCurvePoint, BenchmarkReferences, BuyAndHoldReference
from refinvest.engine.performance import EquityCurvePoint, PerformanceMetrics, PerformanceReport
from refinvest.engine.results import ResultDataIntegrityStatus, assemble_backtest_result
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.ports import SessionCalendarPort


def test_same_market_different_assets_include_signal_reference() -> None:
    payload = _zero_trade_payload(AssetSymbol.QQQ, AssetSymbol.TQQQ)

    assert payload["signalExecutionMarketRelation"] == "SAME_MARKET"
    assert payload["benchmark"]["primary"]["asset"] == "TQQQ"  # type: ignore[index]
    assert payload["benchmark"]["secondaryReference"]["asset"] == "QQQ"  # type: ignore[index]


def test_cross_market_curves_preserve_each_assets_observed_sessions() -> None:
    primary = _reference(
        AssetSymbol.TQQQ,
        ((date(2025, 1, 2), "1"), (date(2025, 1, 6), "1.1")),
    )
    secondary = _reference(
        AssetSymbol.BTCUSDT,
        (
            (date(2025, 1, 2), "1"),
            (date(2025, 1, 3), "1.01"),
            (date(2025, 1, 4), "1.02"),
        ),
    )
    payload = _payload(
        BenchmarkReferences(primary, secondary), AssetSymbol.BTCUSDT, AssetSymbol.TQQQ
    )

    assert payload["signalExecutionMarketRelation"] == "CROSS_MARKET"
    benchmark = cast(dict[str, object], payload["benchmark"])
    assert [point["date"] for point in benchmark["primary"]["equityCurve"]] == [  # type: ignore[index]
        "2025-01-02",
        "2025-01-06",
    ]
    assert [point["date"] for point in benchmark["secondaryReference"]["equityCurve"]] == [  # type: ignore[index]
        "2025-01-02",
        "2025-01-03",
        "2025-01-04",
    ]


def test_same_asset_zero_trade_result_keeps_primary_benchmark_without_secondary() -> None:
    payload = _zero_trade_payload(AssetSymbol.QQQ, AssetSymbol.QQQ)

    assert payload["sampleSizeWarning"] == "ZERO"
    assert payload["signalExecutionMarketRelation"] == "SAME_MARKET"
    assert payload["benchmark"] == {
        "primary": {
            "asset": "QQQ",
            "equityCurve": [
                {"date": "2025-01-02", "value": "1"},
                {"date": "2025-01-03", "value": "1.1"},
            ],
            "totalReturn": "0.1",
            "cagr": None,
            "mdd": "0",
        }
    }


def _zero_trade_payload(
    primary_signal_asset: AssetSymbol, execution_asset: AssetSymbol
) -> dict[str, object]:
    primary = _reference(execution_asset, ((date(2025, 1, 2), "1"), (date(2025, 1, 3), "1.1")))
    secondary = (
        None
        if primary_signal_asset is execution_asset
        else _reference(primary_signal_asset, ((date(2025, 1, 2), "1"), (date(2025, 1, 3), "0.9")))
    )
    return _payload(BenchmarkReferences(primary, secondary), primary_signal_asset, execution_asset)


def _payload(
    benchmark: BenchmarkReferences,
    primary_signal_asset: AssetSymbol,
    execution_asset: AssetSymbol,
) -> dict[str, object]:
    result = assemble_backtest_result(
        PerformanceReport(
            (EquityCurvePoint(date(2025, 1, 2), Decimal("1")),),
            PerformanceMetrics(None, None, None, None, None, 0, None, None, None),
        ),
        (),
        benchmark,
        ResultDataIntegrityStatus("snapshot-1", True, True),
    )
    return _result_payload(
        "run-1",
        result,
        cast(SessionCalendarPort, object()),
        primary_signal_asset,
        execution_asset,
    )


def _reference(asset: AssetSymbol, points: tuple[tuple[date, str], ...]) -> BuyAndHoldReference:
    curve = tuple(BenchmarkCurvePoint(session, Decimal(value)) for session, value in points)
    return BuyAndHoldReference(asset, curve, curve[-1].value - Decimal(1), None, Decimal("0"))
