"""Immutable snapshot-backed Data Explorer series assembly."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from refinvest.application.assets import asset_reference
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.application.snapshot_execution_data import SnapshotExecutionDataLoader
from refinvest.marketdata.ports import require_close


@dataclass(frozen=True)
class SeriesPoint:
    session_date: date
    value: Decimal


def build_series(
    snapshot: DatasetSnapshot,
    loader: SnapshotExecutionDataLoader,
    symbols: tuple[str, ...],
    metric: str,
    start: date,
    end: date,
) -> dict[str, tuple[SeriesPoint, ...]]:
    data = loader.load(snapshot)
    output: dict[str, tuple[SeriesPoint, ...]] = {}
    for symbol in symbols:
        if asset_reference(symbol) is None:
            raise ValueError(f"Unsupported asset: {symbol}")
        sessions = data.calendar.expected_sessions(symbol)
        in_range = tuple(session for session in sessions if start <= session <= end)
        required_sessions = set(in_range)
        if metric == "RETURN":
            required_sessions.update(
                sessions[index - 1]
                for index, session in enumerate(sessions)
                if start <= session <= end and index > 0
            )
        closes = {
            session: require_close(data.market_data.get_close(symbol, session))
            for session in required_sessions
        }
        if metric == "PRICE":
            output[symbol] = tuple(SeriesPoint(session, closes[session]) for session in in_range)
        elif metric == "NORMALIZED":
            if not in_range:
                output[symbol] = ()
            else:
                first = closes[in_range[0]]
                output[symbol] = tuple(
                    SeriesPoint(session, closes[session] / first * 100) for session in in_range
                )
        else:
            values: list[SeriesPoint] = []
            for index, session in enumerate(sessions):
                if start <= session <= end and index:
                    values.append(
                        SeriesPoint(session, closes[session] / closes[sessions[index - 1]] - 1)
                    )
            output[symbol] = tuple(values)
    return output
