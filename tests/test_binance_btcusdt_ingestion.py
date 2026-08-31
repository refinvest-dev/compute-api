from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from refinvest.ingestion.vendors.binance_btcusdt import (
    BinanceBtcusdtDailyBarsClient,
    BinanceBtcusdtIngestionError,
    select_btcusdt_daily_bars,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)


def test_fetches_utc_daily_bar_and_preserves_fractional_btc_volume() -> None:
    requested_urls: list[str] = []

    def download(url: str) -> bytes:
        requested_urls.append(url)
        return (
            b'[[1735776000000,"95000.10","96000.20","94000.30","95500.40",'
            b'"12.34567890",1735862399999,"0",1,"0","0","0"]]'
        )

    bars = BinanceBtcusdtDailyBarsClient(download).fetch_daily_bars(
        date(2025, 1, 2), date(2025, 1, 2)
    )

    query = parse_qs(urlparse(requested_urls[0]).query)
    assert query == {
        "symbol": ["BTCUSDT"],
        "interval": ["1d"],
        "timeZone": ["0"],
        "startTime": ["1735776000000"],
        "endTime": ["1735862399999"],
        "limit": ["1000"],
    }
    assert bars == (
        DailyBar(
            "BTCUSDT",
            date(2025, 1, 2),
            Decimal("95000.10"),
            Decimal("96000.20"),
            Decimal("94000.30"),
            Decimal("95500.40"),
            Decimal("12.34567890"),
        ),
    )


def test_requires_every_expected_btcusdt_session() -> None:
    sessions = {
        AssetSession("BTCUSDT", session_date): CalendarSession(
            CalendarSessionState.EXPECTED, datetime.combine(session_date, datetime.min.time(), UTC)
        )
        for session_date in (date(2025, 1, 2), date(2025, 1, 3))
    }
    bars = (
        DailyBar(
            "BTCUSDT",
            date(2025, 1, 2),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("0"),
        ),
    )

    with pytest.raises(BinanceBtcusdtIngestionError, match="lacks an expected"):
        select_btcusdt_daily_bars(bars, sessions)


@pytest.mark.parametrize(
    "response",
    [
        b'[[1735776000000,"1","1","1","1","1",1735862399999]]',
        b'[[1735776000001,"1","1","1","1","1",1735862400000,"0",1,"0","0","0"]]',
        b'[[1735776000000,"2","1","1","2","1",1735862399999,"0",1,"0","0","0"]]',
    ],
)
def test_rejects_incomplete_non_utc_or_invalid_ohlcv(response: bytes) -> None:
    with pytest.raises(BinanceBtcusdtIngestionError):
        BinanceBtcusdtDailyBarsClient(lambda _: response).fetch_daily_bars(
            date(2025, 1, 2), date(2025, 1, 2)
        )


def test_rejects_download_failure_and_invalid_range() -> None:
    def unavailable(_: str) -> bytes:
        raise OSError("offline")

    with pytest.raises(BinanceBtcusdtIngestionError, match="download failed"):
        BinanceBtcusdtDailyBarsClient(unavailable).fetch_daily_bars(
            date(2025, 1, 2), date(2025, 1, 2)
        )
    with pytest.raises(BinanceBtcusdtIngestionError, match="start must not exceed"):
        BinanceBtcusdtDailyBarsClient().fetch_daily_bars(date(2025, 1, 3), date(2025, 1, 2))
