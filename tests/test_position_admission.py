from datetime import date

from refinvest.engine.entries import EntryCandidate
from refinvest.engine.position import admit_positions
from refinvest.engine.validation.models import AssetSymbol


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
