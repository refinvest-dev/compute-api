"""Cboe VIX historical-close adapter for snapshot ingestion."""

import csv
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.error import URLError
from urllib.request import urlopen

from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
    ReferenceClose,
)

CBOE_VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


class CboeVixIngestionError(ValueError):
    """Cboe's VIX history cannot be fetched or converted without ambiguity."""


class CboeVixHistoricalClient:
    """Fetch Cboe's published VIX history and convert only DATE/CLOSE values."""

    def __init__(self, download: Callable[[str], bytes] | None = None) -> None:
        self._download = download or _download

    def fetch_reference_closes(self) -> tuple[ReferenceClose, ...]:
        try:
            content = self._download(CBOE_VIX_HISTORY_URL)
        except (OSError, URLError) as error:
            raise CboeVixIngestionError("Cboe VIX history download failed.") from error
        return parse_cboe_vix_history(content)

    def fetch_for_calendar(
        self, sessions: Mapping[AssetSession, CalendarSession]
    ) -> tuple[ReferenceClose, ...]:
        """Fetch and require a Cboe close for every VIX expected session in one snapshot."""
        return select_vix_reference_closes(self.fetch_reference_closes(), sessions)


def parse_cboe_vix_history(content: bytes) -> tuple[ReferenceClose, ...]:
    """Convert one complete Cboe VIX CSV response to deterministic canonical closes."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CboeVixIngestionError("Cboe VIX history must be UTF-8 CSV.") from error
    rows = csv.DictReader(StringIO(text))
    if rows.fieldnames != ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]:
        raise CboeVixIngestionError("Cboe VIX history CSV header is unsupported.")
    closes: list[ReferenceClose] = []
    for row in rows:
        if row.get("DATE") is None or row.get("CLOSE") is None:
            raise CboeVixIngestionError("Cboe VIX history row lacks DATE or CLOSE.")
        try:
            session_date = datetime.strptime(row["DATE"], "%m/%d/%Y").date()
            close = Decimal(row["CLOSE"])
            closes.append(ReferenceClose("VIX", session_date, close))
        except (InvalidOperation, ValueError) as error:
            raise CboeVixIngestionError("Cboe VIX history row is invalid.") from error
    if not closes:
        raise CboeVixIngestionError("Cboe VIX history must contain at least one row.")
    ordered = tuple(sorted(closes, key=lambda item: item.session_date))
    if any(
        later.session_date == earlier.session_date
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ):
        raise CboeVixIngestionError("Cboe VIX history contains duplicate session dates.")
    return ordered


def select_vix_reference_closes(
    closes: tuple[ReferenceClose, ...],
    sessions: Mapping[AssetSession, CalendarSession],
) -> tuple[ReferenceClose, ...]:
    """Select snapshot coverage and reject any expected VIX session absent from Cboe data."""
    expected_dates = {
        asset_session.session_date
        for asset_session, session in sessions.items()
        if asset_session.asset == "VIX" and session.state is CalendarSessionState.EXPECTED
    }
    if not expected_dates:
        raise CboeVixIngestionError("Snapshot calendar has no expected VIX sessions.")
    by_date = {item.session_date: item for item in closes}
    missing_dates = expected_dates - by_date.keys()
    if missing_dates:
        raise CboeVixIngestionError("Cboe VIX history lacks an expected VIX session.")
    return tuple(by_date[session_date] for session_date in sorted(expected_dates))


def _download(url: str) -> bytes:
    with urlopen(url, timeout=30) as response:  # noqa: S310 - fixed official Cboe endpoint
        return response.read()
