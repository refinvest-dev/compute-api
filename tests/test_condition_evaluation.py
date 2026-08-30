from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.engine.conditions import evaluate_condition_group
from refinvest.engine.validation.models import (
    AssetSymbol,
    ComparisonOperator,
    Condition,
    LogicalCombinator,
    MetricReference,
    MetricType,
)
from refinvest.marketdata.domain import (
    AssetSession,
    ClosePriceLookup,
    CompletedAssetSession,
    DailyBar,
    DailyBarAvailability,
    DailyBarLookup,
)
from refinvest.marketdata.ports import MissingRequiredMarketDataError


class StaticCalendar:
    def __init__(self, completions: dict[str, tuple[CompletedAssetSession, ...]]) -> None:
        self._completions = completions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return tuple(item.asset_session.session_date for item in self._completions.get(asset, ()))

    def expected_session_completions(self, asset: str) -> tuple[CompletedAssetSession, ...]:
        return self._completions.get(asset, ())


class StaticMarketData:
    def __init__(self, lookups: dict[AssetSession, DailyBarLookup]) -> None:
        self._lookups = lookups

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        return self._lookups[AssetSession(asset, session_date)]

    def get_close(self, asset: str, session_date: date) -> ClosePriceLookup:
        lookup = self.get_daily_bar(asset, session_date)
        return ClosePriceLookup(
            lookup.availability, lookup.bar.close if lookup.bar is not None else None
        )


def completion(asset: str, session_date: date, completed_at: datetime) -> CompletedAssetSession:
    return CompletedAssetSession(AssetSession(asset, session_date), completed_at)


def bar(asset: str, session_date: date, close: str) -> DailyBar:
    return DailyBar(
        asset,
        session_date,
        Decimal("100"),
        Decimal("102"),
        Decimal("99"),
        Decimal(close),
        1000,
    )


def simple(asset: AssetSymbol) -> MetricReference:
    return MetricReference(asset, MetricType.SIMPLE, None)


def condition(
    operator: ComparisonOperator,
    operand_a: MetricReference,
    operand_b: MetricReference | Decimal,
    combinator: LogicalCombinator | None = None,
) -> Condition:
    return Condition(operator, combinator, operand_a, operand_b)


def test_evaluates_exact_decimal_literal_comparisons() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "100.10")
    calendar = StaticCalendar(
        {"QQQ": (completion("QQQ", qqq_bar.session_date, datetime(2025, 1, 3, 21, tzinfo=UTC)),)}
    )
    market_data = StaticMarketData(
        {qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar)}
    )

    assert evaluate_condition_group(
        calendar,
        market_data,
        AssetSymbol.QQQ,
        qqq_bar.session_date,
        (condition(ComparisonOperator.GTE, simple(AssetSymbol.QQQ), Decimal("100.10")),),
    )
    assert not evaluate_condition_group(
        calendar,
        market_data,
        AssetSymbol.QQQ,
        qqq_bar.session_date,
        (condition(ComparisonOperator.LT, simple(AssetSymbol.QQQ), Decimal("100.10")),),
    )


def test_relative_cross_calendar_condition_uses_metric_dispatch() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "500")
    vix_bar = bar("VIX", date(2025, 1, 3), "18")
    calendar = StaticCalendar(
        {
            "BTCUSDT": (completion("BTCUSDT", date(2025, 1, 4), datetime(2025, 1, 5, tzinfo=UTC)),),
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
            "VIX": (completion("VIX", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )
    market_data = StaticMarketData(
        {
            qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar),
            vix_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, vix_bar),
        }
    )

    assert evaluate_condition_group(
        calendar,
        market_data,
        AssetSymbol.BTCUSDT,
        date(2025, 1, 4),
        (condition(ComparisonOperator.GT, simple(AssetSymbol.QQQ), simple(AssetSymbol.VIX)),),
    )


@pytest.mark.parametrize(
    ("combinator", "expected"),
    [(LogicalCombinator.AND, False), (LogicalCombinator.OR, True)],
)
def test_combines_a_uniform_condition_group(combinator: LogicalCombinator, expected: bool) -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "100")
    vix_bar = bar("VIX", date(2025, 1, 3), "18")
    calendar = StaticCalendar(
        {
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
            "VIX": (completion("VIX", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )
    market_data = StaticMarketData(
        {
            qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar),
            vix_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, vix_bar),
        }
    )
    conditions = (
        condition(ComparisonOperator.GT, simple(AssetSymbol.QQQ), Decimal("200")),
        condition(ComparisonOperator.LT, simple(AssetSymbol.VIX), Decimal("20"), combinator),
    )

    assert (
        evaluate_condition_group(
            calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 3), conditions
        )
        is expected
    )


def test_true_or_condition_does_not_hide_later_missing_data() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "100")
    calendar = StaticCalendar(
        {
            "QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
            "VIX": (completion("VIX", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),),
        }
    )
    market_data = StaticMarketData(
        {
            qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar),
            AssetSession("VIX", date(2025, 1, 3)): DailyBarLookup(
                DailyBarAvailability.MISSING_EXPECTED_SESSION
            ),
        }
    )
    conditions = (
        condition(ComparisonOperator.GT, simple(AssetSymbol.QQQ), Decimal("1")),
        condition(
            ComparisonOperator.LT,
            simple(AssetSymbol.VIX),
            Decimal("20"),
            LogicalCombinator.OR,
        ),
    )

    with pytest.raises(MissingRequiredMarketDataError):
        evaluate_condition_group(
            calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 3), conditions
        )


def test_condition_group_is_deterministic_for_the_same_snapshot_inputs() -> None:
    qqq_bar = bar("QQQ", date(2025, 1, 3), "100")
    calendar = StaticCalendar(
        {"QQQ": (completion("QQQ", date(2025, 1, 3), datetime(2025, 1, 3, 21, tzinfo=UTC)),)}
    )
    market_data = StaticMarketData(
        {qqq_bar.asset_session: DailyBarLookup(DailyBarAvailability.AVAILABLE, qqq_bar)}
    )
    conditions = (condition(ComparisonOperator.GTE, simple(AssetSymbol.QQQ), Decimal("100")),)

    assert [
        evaluate_condition_group(
            calendar, market_data, AssetSymbol.QQQ, date(2025, 1, 3), conditions
        )
        for _ in range(2)
    ] == [True, True]
