from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from refinvest.ingestion.vendors.cboe_vix import (
    CBOE_VIX_HISTORY_URL,
    CboeVixHistoricalClient,
    CboeVixIngestionError,
    parse_cboe_vix_history,
    select_vix_reference_closes,
)
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
)


def test_parses_only_cboe_vix_date_and_close_into_ordered_reference_closes() -> None:
    result = parse_cboe_vix_history(
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/03/2025,18,19,17,18.50\n01/02/2025,17,18,16,17.25\n"
    )

    assert [(item.asset, item.session_date, item.close) for item in result] == [
        ("VIX", date(2025, 1, 2), Decimal("17.25")),
        ("VIX", date(2025, 1, 3), Decimal("18.50")),
    ]


def test_client_uses_the_fixed_official_cboe_history_url() -> None:
    requested_urls: list[str] = []

    def download(url: str) -> bytes:
        requested_urls.append(url)
        return b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2025,17,18,16,17.25\n"

    result = CboeVixHistoricalClient(download).fetch_reference_closes()

    assert requested_urls == [CBOE_VIX_HISTORY_URL]
    assert result[0].close == Decimal("17.25")


@pytest.mark.parametrize(
    "content",
    [
        b"DATE,CLOSE\n01/02/2025,17.25\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2025,17,18,16,not-a-number\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2025,17,18,16,17\n01/02/2025,17,18,16,18\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n",
    ],
)
def test_rejects_malformed_or_ambiguous_cboe_history(content: bytes) -> None:
    with pytest.raises(CboeVixIngestionError):
        parse_cboe_vix_history(content)


def test_download_failure_is_not_silently_replaced() -> None:
    def unavailable(_: str) -> bytes:
        raise OSError("offline")

    with pytest.raises(CboeVixIngestionError, match="download failed"):
        CboeVixHistoricalClient(unavailable).fetch_reference_closes()


def test_selects_only_snapshot_coverage_and_rejects_expected_vix_gap() -> None:
    closes = parse_cboe_vix_history(
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2025,17,18,16,17.25\n01/03/2025,18,19,17,18.50\n"
    )
    sessions = {
        AssetSession("VIX", date(2025, 1, 2)): CalendarSession(
            CalendarSessionState.EXPECTED, datetime(2025, 1, 2, 21, tzinfo=UTC)
        )
    }

    assert select_vix_reference_closes(closes, sessions) == (closes[0],)

    with pytest.raises(CboeVixIngestionError, match="lacks an expected"):
        select_vix_reference_closes(
            closes,
            {
                **sessions,
                AssetSession("VIX", date(2025, 1, 6)): CalendarSession(
                    CalendarSessionState.EXPECTED, datetime(2025, 1, 6, 21, tzinfo=UTC)
                ),
            },
        )
