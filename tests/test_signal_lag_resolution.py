from datetime import date

import pytest

from refinvest.engine.entries import EntryCandidate, resolve_entry_candidates
from refinvest.engine.signals import Signal
from refinvest.engine.temporal import TemporalResolutionError
from refinvest.engine.validation.models import AssetSymbol


class StaticCalendar:
    def __init__(self, sessions: dict[str, tuple[date, ...]]) -> None:
        self._sessions = sessions

    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return self._sessions.get(asset, ())

    def expected_session_completions(self, asset: str) -> tuple[object, ...]:
        return ()


def test_zero_lag_preserves_the_signal_session() -> None:
    signal = Signal(AssetSymbol.QQQ, date(2025, 1, 3))
    calendar = StaticCalendar({"QQQ": (date(2025, 1, 3), date(2025, 1, 6))})

    assert resolve_entry_candidates(calendar, AssetSymbol.QQQ, 0, (signal,)) == (
        EntryCandidate(AssetSymbol.QQQ, date(2025, 1, 3), date(2025, 1, 3)),
    )


def test_lag_moves_to_the_next_primary_signal_asset_session() -> None:
    signal = Signal(AssetSymbol.QQQ, date(2025, 1, 3))
    calendar = StaticCalendar({"QQQ": (date(2025, 1, 3), date(2025, 1, 6))})

    assert resolve_entry_candidates(calendar, AssetSymbol.QQQ, 1, (signal,)) == (
        EntryCandidate(AssetSymbol.QQQ, date(2025, 1, 3), date(2025, 1, 6)),
    )


def test_btc_weekend_lag_uses_the_btc_signal_calendar() -> None:
    signal = Signal(AssetSymbol.BTCUSDT, date(2025, 1, 4))
    calendar = StaticCalendar(
        {"BTCUSDT": (date(2025, 1, 4), date(2025, 1, 5), date(2025, 1, 6))}
    )

    assert resolve_entry_candidates(calendar, AssetSymbol.BTCUSDT, 1, (signal,)) == (
        EntryCandidate(AssetSymbol.BTCUSDT, date(2025, 1, 4), date(2025, 1, 5)),
    )


def test_lag_beyond_the_pinned_calendar_fails_fast() -> None:
    signal = Signal(AssetSymbol.QQQ, date(2025, 1, 3))
    calendar = StaticCalendar({"QQQ": (date(2025, 1, 3),)})

    with pytest.raises(TemporalResolutionError) as error:
        resolve_entry_candidates(calendar, AssetSymbol.QQQ, 1, (signal,))

    assert error.value.error_code == "CALENDAR_RESOLUTION_FAILED"


def test_rejects_signals_from_another_asset_or_an_unordered_sequence() -> None:
    calendar = StaticCalendar(
        {"QQQ": (date(2025, 1, 3), date(2025, 1, 6)), "VIX": (date(2025, 1, 3),)}
    )

    with pytest.raises(ValueError, match="explicit Primary Signal Asset"):
        resolve_entry_candidates(
            calendar, AssetSymbol.QQQ, 0, (Signal(AssetSymbol.VIX, date(2025, 1, 3)),)
        )
    with pytest.raises(ValueError, match="strictly ascending"):
        resolve_entry_candidates(
            calendar,
            AssetSymbol.QQQ,
            0,
            (
                Signal(AssetSymbol.QQQ, date(2025, 1, 6)),
                Signal(AssetSymbol.QQQ, date(2025, 1, 3)),
            ),
        )


def test_lag_resolution_is_deterministic_for_the_same_snapshot_inputs() -> None:
    signals = (Signal(AssetSymbol.QQQ, date(2025, 1, 3)),)
    calendar = StaticCalendar({"QQQ": (date(2025, 1, 3), date(2025, 1, 6))})

    assert [
        resolve_entry_candidates(calendar, AssetSymbol.QQQ, 1, signals)
        for _ in range(2)
    ] == [
        (EntryCandidate(AssetSymbol.QQQ, date(2025, 1, 3), date(2025, 1, 6)),),
        (EntryCandidate(AssetSymbol.QQQ, date(2025, 1, 3), date(2025, 1, 6)),),
    ]
