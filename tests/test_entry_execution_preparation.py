from datetime import date
from decimal import Decimal

import pytest

from refinvest.engine.entries import EntryCandidate
from refinvest.engine.execution import EntryExecution, prepare_entry_executions
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.domain import AssetSession, DailyBar, DailyBarAvailability, DailyBarLookup
from refinvest.marketdata.ports import MissingRequiredMarketDataError


class StaticCalendar:
    def __init__(self, sessions: dict[str, tuple[date, ...]]) -> None:
        self.sessions = sessions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return self.sessions.get(asset, ())

    def expected_session_completions(self, asset: str) -> tuple[object, ...]:
        return ()


class StaticMarketData:
    def __init__(self, bars: dict[AssetSession, DailyBarLookup]) -> None:
        self.bars = bars

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        return self.bars[AssetSession(asset, session_date)]


def bar(asset: str, session_date: date, open_price: str) -> DailyBar:
    return DailyBar(
        asset, session_date, Decimal(open_price), Decimal("110"), Decimal("90"), Decimal("100"), 1
    )


def candidate() -> EntryCandidate:
    return EntryCandidate(AssetSymbol.BTCUSDT, date(2025, 1, 4), date(2025, 1, 5))


def test_resolves_next_execution_session_and_buy_slippage() -> None:
    execution_bar = bar("TQQQ", date(2025, 1, 6), "100")
    result = prepare_entry_executions(
        StaticCalendar({"TQQQ": (date(2025, 1, 6),)}),
        StaticMarketData(
            {
                execution_bar.asset_session: DailyBarLookup(
                    DailyBarAvailability.AVAILABLE, execution_bar
                )
            }
        ),
        AssetSymbol.TQQQ,
        Decimal("0.001"),
        (candidate(),),
    )

    assert result == (
        EntryExecution(
            candidate(),
            AssetSymbol.TQQQ,
            date(2025, 1, 6),
            Decimal("100"),
            Decimal("100.100"),
        ),
    )


def test_missing_execution_bar_fails_fast() -> None:
    with pytest.raises(MissingRequiredMarketDataError):
        prepare_entry_executions(
            StaticCalendar({"TQQQ": (date(2025, 1, 6),)}),
            StaticMarketData({
                AssetSession("TQQQ", date(2025, 1, 6)): DailyBarLookup(
                    DailyBarAvailability.MISSING_EXPECTED_SESSION
                )
            }),
            AssetSymbol.TQQQ,
            Decimal("0"),
            (candidate(),),
        )


def test_execution_session_after_calendar_end_fails_fast() -> None:
    with pytest.raises(TemporalResolutionError) as error:
        prepare_entry_executions(
            StaticCalendar({"TQQQ": ()}),
            StaticMarketData({}),
            AssetSymbol.TQQQ,
            Decimal("0"),
            (candidate(),),
        )

    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"
