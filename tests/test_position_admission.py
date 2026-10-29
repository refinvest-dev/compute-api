from datetime import date
from decimal import Decimal

from refinvest.engine.entries import EntryCandidate
from refinvest.engine.execution import price_entry_execution_schedules
from refinvest.engine.position import admit_positions
from refinvest.engine.validation.models import AssetSymbol
from refinvest.marketdata.domain import AssetSession, DailyBar, DailyBarAvailability, DailyBarLookup


class StaticCalendar:
    def __init__(self, sessions: dict[str, tuple[date, ...]]) -> None:
        self.sessions = sessions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return self.sessions[asset]

    def expected_session_completions(self, asset: str) -> tuple[object, ...]:
        return ()


def candidate(signal: date, entry_signal: date) -> EntryCandidate:
    return EntryCandidate(AssetSymbol.QQQ, signal, entry_signal)


def test_ignores_overlapping_and_same_session_entry_candidates() -> None:
    calendar = StaticCalendar(
        {
            "QQQ": tuple(date(2025, 1, day) for day in range(2, 11)),
            "TQQQ": tuple(date(2025, 1, day) for day in range(2, 12)),
        }
    )
    positions = admit_positions(
        calendar,
        AssetSymbol.TQQQ,
        2,
        (
            candidate(date(2025, 1, 2), date(2025, 1, 2)),
            candidate(date(2025, 1, 3), date(2025, 1, 3)),
            candidate(date(2025, 1, 4), date(2025, 1, 4)),
            candidate(date(2025, 1, 7), date(2025, 1, 7)),
        ),
    )

    assert [position.entry.candidate.signal_session for position in positions] == [
        date(2025, 1, 2),
        date(2025, 1, 7),
    ]


def test_duplicate_candidates_do_not_trigger_daily_bar_reads() -> None:
    calendar = StaticCalendar(
        {
            "QQQ": tuple(date(2025, 1, day) for day in range(2, 8)),
            "TQQQ": tuple(date(2025, 1, day) for day in range(2, 9)),
        }
    )
    positions = admit_positions(
        calendar,
        AssetSymbol.TQQQ,
        2,
        (
            candidate(date(2025, 1, 2), date(2025, 1, 2)),
            candidate(date(2025, 1, 3), date(2025, 1, 3)),
        ),
    )
    accepted_schedule = positions[0].entry
    accepted_bar = DailyBar(
        "TQQQ",
        accepted_schedule.execution_session,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        1,
    )

    class AcceptedOnlyMarketData:
        def get_daily_bar(self, asset: str, session_date: date) -> DailyBarLookup:
            if AssetSession(asset, session_date) != accepted_bar.asset_session:
                raise AssertionError("A duplicate candidate Daily Bar must not be read.")
            return DailyBarLookup(DailyBarAvailability.AVAILABLE, accepted_bar)

    priced = price_entry_execution_schedules(
        AcceptedOnlyMarketData(), Decimal("0"), tuple(position.entry for position in positions)
    )

    assert len(priced) == 1
