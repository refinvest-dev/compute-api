from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.ingestion.vendors.massive_etf import (
    MVP_ETF_ASSETS,
    MassiveEtfDailyBarsClient,
    MassiveEtfIngestionError,
    select_etf_daily_bars,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)


def test_fetches_split_adjusted_daily_bars_from_fixed_massive_endpoint() -> None:
    requested_urls: list[str] = []

    def download(url: str) -> bytes:
        requested_urls.append(url)
        return (
            b'{"adjusted":true,"status":"OK","ticker":"QQQ","results":['
            b'{"t":1735866000000,"o":500.1,"h":502.5,"l":499.5,"c":501.2,"v":1234}]}'
        )

    bars = MassiveEtfDailyBarsClient("test-key", download).fetch_daily_bars(
        "QQQ", date(2025, 1, 2), date(2025, 1, 2)
    )

    assert "adjusted=true" in requested_urls[0]
    assert "sort=asc" in requested_urls[0]
    assert "apiKey=test-key" in requested_urls[0]
    assert bars == (
        DailyBar(
            "QQQ",
            date(2025, 1, 2),
            Decimal("500.1"),
            Decimal("502.5"),
            Decimal("499.5"),
            Decimal("501.2"),
            1234,
        ),
    )


def test_requires_expected_session_bars_for_every_mvp_etf() -> None:
    sessions = {
        AssetSession(asset, date(2025, 1, 2)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
        )
        for asset in MVP_ETF_ASSETS
    }
    bars = tuple(
        DailyBar(asset, date(2025, 1, 2), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), 1)
        for asset in MVP_ETF_ASSETS
    )

    assert select_etf_daily_bars(bars, sessions) == tuple(
        sorted(bars, key=lambda item: (item.asset, item.session_date))
    )

    with pytest.raises(MassiveEtfIngestionError, match="lacks an expected"):
        select_etf_daily_bars(bars[:-1], sessions)


@pytest.mark.parametrize(
    "response",
    [
        b'{"adjusted":false,"status":"OK","ticker":"QQQ","results":[]}',
        b'{"adjusted":true,"status":"OK","ticker":"SPY","results":[]}',
        b'{"adjusted":true,"status":"OK","ticker":"QQQ","results":[{"t":1735866000000,"o":500,"h":499,"l":498,"c":501,"v":1}]}',
    ],
)
def test_rejects_unadjusted_wrong_ticker_or_invalid_ohlcv(response: bytes) -> None:
    with pytest.raises(MassiveEtfIngestionError):
        MassiveEtfDailyBarsClient("test-key", lambda _: response).fetch_daily_bars(
            "QQQ", date(2025, 1, 2), date(2025, 1, 2)
        )


def test_rejects_unsupported_asset_and_download_failure() -> None:
    with pytest.raises(MassiveEtfIngestionError, match="unsupported"):
        MassiveEtfDailyBarsClient("test-key").fetch_daily_bars(
            "VIX", date(2025, 1, 2), date(2025, 1, 2)
        )

    def unavailable(_: str) -> bytes:
        raise OSError("offline")

    with pytest.raises(MassiveEtfIngestionError, match="download failed"):
        MassiveEtfDailyBarsClient("test-key", unavailable).fetch_daily_bars(
            "QQQ", date(2025, 1, 2), date(2025, 1, 2)
        )
