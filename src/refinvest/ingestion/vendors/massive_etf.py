"""Massive split-adjusted ETF Daily Bar adapter for snapshot ingestion."""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)

MASSIVE_STOCKS_BASE_URL = "https://api.massive.com"
MVP_ETF_ASSETS = ("QQQ", "SPY", "TQQQ", "SOXL")
_NEW_YORK = ZoneInfo("America/New_York")


class MassiveEtfIngestionError(ValueError):
    """Massive ETF data cannot be fetched or converted without ambiguity."""


class MassiveEtfDailyBarsClient:
    """Fetch Massive split-adjusted daily stock aggregates for the MVP ETFs."""

    def __init__(self, api_key: str, download: Callable[[str], bytes] | None = None) -> None:
        if not api_key.strip():
            raise ValueError("Massive API key must not be empty.")
        self._api_key = api_key
        self._download = download or _download

    def fetch_daily_bars(self, asset: str, start: date, end: date) -> tuple[DailyBar, ...]:
        if asset not in MVP_ETF_ASSETS:
            raise MassiveEtfIngestionError("Massive ETF asset is unsupported.")
        if start > end:
            raise MassiveEtfIngestionError("ETF range start must not exceed end.")
        next_url = _aggregate_url(asset, start, end, self._api_key)
        bars: list[DailyBar] = []
        while next_url is not None:
            try:
                content = self._download(next_url)
            except (OSError, URLError) as error:
                raise MassiveEtfIngestionError("Massive ETF aggregate download failed.") from error
            page, next_url = _parse_aggregate_page(content, asset)
            bars.extend(page)
            if next_url is not None:
                next_url = _with_api_key(next_url, self._api_key)
        return _ordered_unique_bars(bars)

    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> tuple[DailyBar, ...]:
        """Fetch every MVP ETF and require data for all expected snapshot sessions."""
        bars = tuple(
            bar for asset in MVP_ETF_ASSETS for bar in self.fetch_daily_bars(asset, start, end)
        )
        return select_etf_daily_bars(bars, sessions)


def select_etf_daily_bars(
    bars: tuple[DailyBar, ...], sessions: Mapping[AssetSession, CalendarSession]
) -> tuple[DailyBar, ...]:
    """Select calendar coverage and reject every expected ETF session gap before publication."""
    expected = {
        asset_session
        for asset_session, session in sessions.items()
        if asset_session.asset in MVP_ETF_ASSETS and session.state is CalendarSessionState.EXPECTED
    }
    if not expected:
        raise MassiveEtfIngestionError("Snapshot calendar has no expected ETF sessions.")
    by_session = {bar.asset_session: bar for bar in bars}
    if len(by_session) != len(bars):
        raise MassiveEtfIngestionError("Massive ETF data contains duplicate asset sessions.")
    missing = expected - by_session.keys()
    if missing:
        raise MassiveEtfIngestionError("Massive ETF data lacks an expected session.")
    return tuple(
        by_session[item]
        for item in sorted(expected, key=lambda item: (item.asset, item.session_date))
    )


def _aggregate_url(asset: str, start: date, end: date, api_key: str) -> str:
    path = f"/v2/aggs/ticker/{asset}/range/1/day/{start.isoformat()}/{end.isoformat()}"
    query = urlencode({"adjusted": "true", "sort": "asc", "limit": "50000", "apiKey": api_key})
    return f"{MASSIVE_STOCKS_BASE_URL}{path}?{query}"


def _with_api_key(url: str, api_key: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.massive.com":
        raise MassiveEtfIngestionError("Massive ETF aggregate next_url is untrusted.")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["apiKey"] = api_key
    return urlunparse(parsed._replace(query=urlencode(query)))


def _parse_aggregate_page(content: bytes, asset: str) -> tuple[tuple[DailyBar, ...], str | None]:
    try:
        payload_object = json.loads(content, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MassiveEtfIngestionError(
            "Massive ETF aggregate response must be valid JSON."
        ) from error
    if not isinstance(payload_object, dict):
        raise MassiveEtfIngestionError("Massive ETF aggregate response must be an object.")
    payload = cast(dict[str, object], payload_object)
    if payload.get("status") != "OK" or payload.get("ticker") != asset:
        raise MassiveEtfIngestionError("Massive ETF aggregate response identity is invalid.")
    if payload.get("adjusted") is not True:
        raise MassiveEtfIngestionError("Massive ETF aggregate response must be split-adjusted.")
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise MassiveEtfIngestionError("Massive ETF aggregate results are invalid.")
    raw_results = cast(list[object], results)
    if any(not isinstance(item, dict) for item in raw_results):
        raise MassiveEtfIngestionError("Massive ETF aggregate results are invalid.")
    typed_results = cast(list[dict[str, object]], raw_results)
    next_url = payload.get("next_url")
    if next_url is not None and not isinstance(next_url, str):
        raise MassiveEtfIngestionError("Massive ETF aggregate next_url is invalid.")
    return tuple(_daily_bar(asset, item) for item in typed_results), next_url


def _daily_bar(asset: str, item: dict[str, object]) -> DailyBar:
    try:
        timestamp = _integer(item, "t")
        session_date = datetime.fromtimestamp(timestamp / 1000, UTC).astimezone(_NEW_YORK).date()
        bar = DailyBar(
            asset,
            session_date,
            _decimal(item, "o"),
            _decimal(item, "h"),
            _decimal(item, "l"),
            _decimal(item, "c"),
            Decimal(_integer(item, "v")),
        )
    except (InvalidOperation, OSError, OverflowError, ValueError) as error:
        raise MassiveEtfIngestionError("Massive ETF aggregate bar is invalid.") from error
    if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0 or bar.volume < 0:
        raise MassiveEtfIngestionError("Massive ETF aggregate bar has invalid OHLCV.")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        raise MassiveEtfIngestionError("Massive ETF aggregate bar violates OHLC bounds.")
    return bar


def _ordered_unique_bars(bars: list[DailyBar]) -> tuple[DailyBar, ...]:
    ordered = tuple(sorted(bars, key=lambda item: item.session_date))
    if any(
        later.session_date == earlier.session_date
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ):
        raise MassiveEtfIngestionError("Massive ETF data contains duplicate session dates.")
    return ordered


def _decimal(payload: dict[str, object], field: str) -> Decimal:
    value = payload.get(field)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise MassiveEtfIngestionError(f"Massive ETF aggregate {field} must be a JSON number.")


def _integer(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MassiveEtfIngestionError(f"Massive ETF aggregate {field} must be an integer.")
    return value


def _download(url: str) -> bytes:
    with urlopen(url, timeout=30) as response:  # noqa: S310 - fixed Massive host and path
        return response.read()
