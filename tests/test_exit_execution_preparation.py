from datetime import date
from decimal import Decimal

import pytest

from refinvest.engine.entries import EntryCandidate, ExitCandidate, resolve_exit_candidates
from refinvest.engine.execution import ExitExecution, prepare_exit_executions
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
    def __init__(self, lookups: dict[AssetSession, DailyBarLookup]) -> None:
        self.lookups = lookups

    def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
        return self.lookups[AssetSession(asset, session_date)]


def entry_candidate() -> EntryCandidate:
    return EntryCandidate(AssetSymbol.BTCUSDT, date(2025, 1, 4), date(2025, 1, 5))


def bar() -> DailyBar:
    return DailyBar(
        "TQQQ", date(2025, 1, 7), Decimal("100"), Decimal("110"), Decimal("90"), Decimal("105"), 1
    )


def test_holding_starts_from_lagged_entry_signal_session() -> None:
    candidate = entry_candidate()
    result = resolve_exit_candidates(
        StaticCalendar({"BTCUSDT": (date(2025, 1, 4), date(2025, 1, 5), date(2025, 1, 6))}),
        1,
        (candidate,),
    )

    assert result == (ExitCandidate(candidate, date(2025, 1, 6)),)


def test_resolves_exit_session_and_sell_slippage() -> None:
    candidate = ExitCandidate(entry_candidate(), date(2025, 1, 6))
    execution_bar = bar()
    result = prepare_exit_executions(
        StaticCalendar({"TQQQ": (date(2025, 1, 7),)}),
        StaticMarketData(
            {
                execution_bar.asset_session: DailyBarLookup(
                    DailyBarAvailability.AVAILABLE, execution_bar
                )
            }
        ),
        AssetSymbol.TQQQ,
        Decimal("0.001"),
        (candidate,),
    )

    assert result == (
        ExitExecution(
            candidate, AssetSymbol.TQQQ, date(2025, 1, 7), Decimal("100"), Decimal("99.900")
        ),
    )


def test_exit_calendar_or_market_data_gaps_fail_fast() -> None:
    candidate = ExitCandidate(entry_candidate(), date(2025, 1, 6))
    with pytest.raises(TemporalResolutionError):
        prepare_exit_executions(
            StaticCalendar({"TQQQ": ()}),
            StaticMarketData({}),
            AssetSymbol.TQQQ,
            Decimal("0"),
            (candidate,),
        )
    with pytest.raises(MissingRequiredMarketDataError):
        prepare_exit_executions(
            StaticCalendar({"TQQQ": (date(2025, 1, 7),)}),
            StaticMarketData(
                {
                    AssetSession("TQQQ", date(2025, 1, 7)): DailyBarLookup(
                        DailyBarAvailability.MISSING_EXPECTED_SESSION
                    )
                }
            ),
            AssetSymbol.TQQQ,
            Decimal("0"),
            (candidate,),
        )
