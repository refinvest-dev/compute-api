"""Binance Spot BTCUSDT UTC daily-bar adapter for snapshot ingestion."""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)

BINANCE_SPOT_BASE_URL = "https://api.binance.com"
BINANCE_BTCUSDT_KLINES_URL = f"{BINANCE_SPOT_BASE_URL}/api/v3/klines"
BTCUSDT = "BTCUSDT"
_DAY_MILLISECONDS = 86_400_000
_MAX_KLINES_PER_REQUEST = 1_000


class BinanceBtcusdtIngestionError(ValueError):
    """Binance BTCUSDT data cannot be fetched or converted without ambiguity."""


class BinanceBtcusdtDailyBarsClient:
    """Fetch Binance Spot BTCUSDT UTC daily Klines without vendor types escaping ingestion."""

    def __init__(self, download: Callable[[str], bytes] | None = None) -> None:
        self._download = download or _download

    def fetch_daily_bars(self, start: date, end: date) -> tuple[DailyBar, ...]:
        if start > end:
            raise BinanceBtcusdtIngestionError("BTCUSDT range start must not exceed end.")
        end_milliseconds = _midnight_milliseconds(end + timedelta(days=1)) - 1
        cursor = _midnight_milliseconds(start)
        bars: list[DailyBar] = []
        while cursor <= end_milliseconds:
            try:
                content = self._download(_klines_url(cursor, end_milliseconds))
            except (OSError, URLError) as error:
                raise BinanceBtcusdtIngestionError(
                    "Binance BTCUSDT Kline download failed."
                ) from error
            page = _parse_klines(content)
            if not page:
                break
            bars.extend(page)
            cursor = _midnight_milliseconds(page[-1].session_date + timedelta(days=1))
            if len(page) < _MAX_KLINES_PER_REQUEST:
                break
        return _ordered_unique_bars(bars)

    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]:
        """Fetch and require BTCUSDT data for every expected snapshot session."""
        return select_btcusdt_daily_bars(self.fetch_daily_bars(start, end), sessions)


def select_btcusdt_daily_bars(
    bars: tuple[DailyBar, ...], sessions: Mapping[AssetSession, CalendarSession]
) -> tuple[DailyBar, ...]:
    """Select snapshot coverage and reject every BTCUSDT expected-session gap."""
    expected = {
        asset_session
        for asset_session, session in sessions.items()
        if asset_session.asset == BTCUSDT and session.state is CalendarSessionState.EXPECTED
    }
    if not expected:
        raise BinanceBtcusdtIngestionError("Snapshot calendar has no expected BTCUSDT sessions.")
    by_session = {bar.asset_session: bar for bar in bars}
    if len(by_session) != len(bars):
        raise BinanceBtcusdtIngestionError(
            "Binance BTCUSDT data contains duplicate asset sessions."
        )
    missing = expected - by_session.keys()
    if missing:
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT data lacks an expected session.")
    return tuple(by_session[item] for item in sorted(expected, key=lambda item: item.session_date))


def _klines_url(start_milliseconds: int, end_milliseconds: int) -> str:
    query = urlencode(
        {
            "symbol": BTCUSDT,
            "interval": "1d",
            "timeZone": "0",
            "startTime": start_milliseconds,
            "endTime": end_milliseconds,
            "limit": _MAX_KLINES_PER_REQUEST,
        }
    )
    return f"{BINANCE_BTCUSDT_KLINES_URL}?{query}"


def _parse_klines(content: bytes) -> tuple[DailyBar, ...]:
    try:
        payload_object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BinanceBtcusdtIngestionError(
            "Binance BTCUSDT Kline response must be valid JSON."
        ) from error
    if not isinstance(payload_object, list):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline response must be an array.")
    return tuple(_daily_bar(item) for item in cast(list[object], payload_object))


def _daily_bar(item: object) -> DailyBar:
    if not isinstance(item, list):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline row must contain 12 fields.")
    row = cast(list[object], item)
    if len(row) != 12:
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline row must contain 12 fields.")
    try:
        open_time = _integer(row[0])
        close_time = _integer(row[6])
        if open_time % _DAY_MILLISECONDS != 0 or close_time != open_time + _DAY_MILLISECONDS - 1:
            raise BinanceBtcusdtIngestionError(
                "Binance BTCUSDT Kline is not a complete UTC daily bar."
            )
        session_date = datetime.fromtimestamp(open_time / 1000, UTC).date()
        bar = DailyBar(
            BTCUSDT,
            session_date,
            _decimal_string(row[1]),
            _decimal_string(row[2]),
            _decimal_string(row[3]),
            _decimal_string(row[4]),
            _decimal_string(row[5]),
        )
    except (InvalidOperation, OSError, OverflowError, ValueError) as error:
        if isinstance(error, BinanceBtcusdtIngestionError):
            raise
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline row is invalid.") from error
    if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0 or bar.volume < 0:
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline has invalid OHLCV.")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline violates OHLC bounds.")
    return bar


def _ordered_unique_bars(bars: list[DailyBar]) -> tuple[DailyBar, ...]:
    ordered = tuple(sorted(bars, key=lambda item: item.session_date))
    if any(
        later.session_date == earlier.session_date
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT data contains duplicate session dates.")
    return ordered


def _midnight_milliseconds(session_date: date) -> int:
    return int(datetime.combine(session_date, datetime.min.time(), UTC).timestamp() * 1000)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline timestamp must be an integer.")
    return value


def _decimal_string(value: object) -> Decimal:
    if not isinstance(value, str):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline numeric field must be a string.")
    return Decimal(value)


def _download(url: str) -> bytes:
    with urlopen(url, timeout=30) as response:  # noqa: S310 - fixed official Binance host and path
        return response.read()
