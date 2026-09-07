"""Verified Binance Public Data BTCUSDT Spot daily archive ingestion."""

import csv
import hashlib
import re
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen

from refinvest.application.dataset_snapshots import SourceArtifact
from refinvest.marketdata.domain import (
    AssetSession,
    CalendarSession,
    CalendarSessionState,
    DailyBar,
)

BINANCE_PUBLIC_DATA_BASE_URL = "https://data.binance.vision"
BTCUSDT = "BTCUSDT"
_CHECKSUM = re.compile(r"^([0-9a-fA-F]{64})\s+\*?([^\s]+)\s*$")
_REQUIRED_COLUMNS = frozenset({"open_time", "open", "high", "low", "close", "volume"})


class BinanceBtcusdtIngestionError(ValueError):
    """A Binance archive cannot be verified and normalized without ambiguity."""


@dataclass(frozen=True)
class BtcusdtArchiveBatch:
    """Canonical bars plus immutable evidence for the archives that produced them."""

    daily_bars: tuple[DailyBar, ...]
    source_artifacts: tuple[SourceArtifact, ...]


class BinanceArchiveRevisionStore(Protocol):
    """Durable normalized archive revisions, keyed by immutable URI and SHA-256."""

    def get(self, uri: str, sha256: str) -> BtcusdtArchiveBatch | None: ...

    def put_if_absent(self, batch: BtcusdtArchiveBatch) -> BtcusdtArchiveBatch: ...


class BinanceBtcusdtDailyBarsClient:
    """Fetch only completed Binance Spot BTCUSDT 1d Public Data archives."""

    def __init__(
        self,
        download: Callable[[str], bytes] | None = None,
        *,
        base_url: str = BINANCE_PUBLIC_DATA_BASE_URL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        revisions: BinanceArchiveRevisionStore | None = None,
    ) -> None:
        self._download = download or _download
        self._base_url = base_url.rstrip("/")
        self._now = now
        self._verified_archives: dict[tuple[str, str], BtcusdtArchiveBatch] = {}
        self._revisions = revisions or _InMemoryArchiveRevisionStore()

    def fetch_daily_bars(self, start: date, end: date) -> BtcusdtArchiveBatch:
        """Use monthly archives for completed months and daily archives for the current UTC month."""  # noqa: E501
        if start > end:
            raise BinanceBtcusdtIngestionError("BTCUSDT range start must not exceed end.")
        current = self._now()
        if current.tzinfo is not UTC:
            raise BinanceBtcusdtIngestionError(
                "Binance archive clock must be an exact UTC datetime."
            )
        if end >= current.date():
            raise BinanceBtcusdtIngestionError(
                "Current or future UTC daily archives must not be ingested."
            )
        uris = self._archive_uris(start, end, current.date())
        batches = tuple(self._fetch_archive(uri) for uri in uris)
        return BtcusdtArchiveBatch(
            tuple(
                bar
                for bar in _ordered_unique_bars(
                    [bar for batch in batches for bar in batch.daily_bars]
                )
                if start <= bar.session_date <= end
            ),
            tuple(artifact for batch in batches for artifact in batch.source_artifacts),
        )

    def fetch_for_calendar(
        self,
        sessions: Mapping[AssetSession, CalendarSession],
        start: date,
        end: date,
    ) -> BtcusdtArchiveBatch:
        batch = self.fetch_daily_bars(start, end)
        return BtcusdtArchiveBatch(
            select_btcusdt_daily_bars(batch.daily_bars, sessions), batch.source_artifacts
        )

    def _archive_uris(self, start: date, end: date, today: date) -> tuple[str, ...]:
        current_month = today.replace(day=1)
        uris: list[str] = []
        cursor = start
        while cursor <= end:
            month_end = _month_end(cursor)
            segment_end = min(month_end, end)
            if cursor.replace(day=1) < current_month:
                uris.append(
                    f"{self._base_url}/data/spot/monthly/klines/{BTCUSDT}/1d/"
                    f"{BTCUSDT}-1d-{cursor:%Y-%m}.zip"
                )
            else:
                uris.extend(
                    f"{self._base_url}/data/spot/daily/klines/{BTCUSDT}/1d/"
                    f"{BTCUSDT}-1d-{day:%Y-%m-%d}.zip"
                    for day in _dates(cursor, segment_end)
                )
            cursor = segment_end + timedelta(days=1)
        return tuple(uris)

    def _fetch_archive(self, uri: str) -> BtcusdtArchiveBatch:
        try:
            archive = self._download(uri)
            checksum = self._download(f"{uri}.CHECKSUM")
        except (OSError, URLError) as error:
            raise BinanceBtcusdtIngestionError(
                "Binance Public Data archive download failed."
            ) from error
        expected_hash = _checksum_sha256(checksum, uri)
        actual_hash = hashlib.sha256(archive).hexdigest()
        if actual_hash != expected_hash:
            raise BinanceBtcusdtIngestionError("Binance Public Data archive SHA-256 mismatch.")
        cache_key = (uri, actual_hash)
        cached = self._verified_archives.get(cache_key)
        if cached is not None:
            return cached
        persisted = self._revisions.get(*cache_key)
        if persisted is not None:
            self._verified_archives[cache_key] = persisted
            return persisted
        batch = BtcusdtArchiveBatch(
            _parse_archive_csv(archive),
            (SourceArtifact(uri, actual_hash, self._retrieved_at()),),
        )
        persisted = self._revisions.put_if_absent(batch)
        self._verified_archives[cache_key] = persisted
        return persisted

    def _retrieved_at(self) -> datetime:
        current = self._now()
        if current.tzinfo is not UTC:
            raise BinanceBtcusdtIngestionError(
                "Binance archive clock must be an exact UTC datetime."
            )
        return current


def select_btcusdt_daily_bars(
    bars: tuple[DailyBar, ...], sessions: Mapping[AssetSession, CalendarSession]
) -> tuple[DailyBar, ...]:
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


def _checksum_sha256(content: bytes, archive_uri: str) -> str:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise BinanceBtcusdtIngestionError("Binance archive CHECKSUM must be UTF-8.") from error
    if len(lines) != 1 or (match := _CHECKSUM.fullmatch(lines[0])) is None:
        raise BinanceBtcusdtIngestionError("Binance archive CHECKSUM format is invalid.")
    if match.group(2) != archive_uri.rsplit("/", maxsplit=1)[-1]:
        raise BinanceBtcusdtIngestionError("Binance archive CHECKSUM names a different file.")
    return match.group(1).lower()


def _parse_archive_csv(content: bytes) -> tuple[DailyBar, ...]:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            members = [name for name in archive.namelist() if not name.endswith("/")]
            if len(members) != 1 or not members[0].lower().endswith(".csv"):
                raise BinanceBtcusdtIngestionError(
                    "Binance archive must contain exactly one CSV file."
                )
            with archive.open(members[0]) as raw:
                rows = csv.DictReader(TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                if rows.fieldnames is None or not _REQUIRED_COLUMNS <= set(rows.fieldnames):
                    raise BinanceBtcusdtIngestionError("Binance Kline CSV lacks required columns.")
                return _ordered_unique_bars([_daily_bar(row) for row in rows])
    except BinanceBtcusdtIngestionError:
        raise
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        raise BinanceBtcusdtIngestionError("Binance archive ZIP or CSV is invalid.") from error


def _daily_bar(row: Mapping[str, str | None]) -> DailyBar:
    try:
        session_date = _utc_session_date(_timestamp(_field(row, "open_time")))
        bar = DailyBar(
            BTCUSDT,
            session_date,
            Decimal(_field(row, "open")),
            Decimal(_field(row, "high")),
            Decimal(_field(row, "low")),
            Decimal(_field(row, "close")),
            Decimal(_field(row, "volume")),
        )
    except (InvalidOperation, ValueError, OverflowError, OSError) as error:
        if isinstance(error, BinanceBtcusdtIngestionError):
            raise
        raise BinanceBtcusdtIngestionError("Binance Kline CSV row is invalid.") from error
    if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0 or bar.volume < 0:
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline has invalid OHLCV.")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT Kline violates OHLC bounds.")
    return bar


def _field(row: Mapping[str, str | None], name: str) -> str:
    value = row.get(name)
    if value is None or not value.strip():
        raise BinanceBtcusdtIngestionError(f"Binance Kline CSV {name} is missing.")
    return value


def _timestamp(value: str) -> int:
    try:
        timestamp = int(value)
    except ValueError as error:
        raise BinanceBtcusdtIngestionError("Binance Kline open_time must be an integer.") from error
    if timestamp < 0:
        raise BinanceBtcusdtIngestionError("Binance Kline open_time must be non-negative.")
    return timestamp


def _utc_session_date(timestamp: int) -> date:
    divisor = 1_000_000 if timestamp >= 100_000_000_000_000 else 1_000
    instant = datetime.fromtimestamp(timestamp / divisor, UTC)
    if (instant.hour, instant.minute, instant.second, instant.microsecond) != (0, 0, 0, 0):
        raise BinanceBtcusdtIngestionError("Binance Kline is not a UTC daily bar.")
    return instant.date()


def _ordered_unique_bars(bars: list[DailyBar]) -> tuple[DailyBar, ...]:
    ordered = tuple(sorted(bars, key=lambda item: item.session_date))
    if any(
        later.session_date == earlier.session_date
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ):
        raise BinanceBtcusdtIngestionError("Binance BTCUSDT data contains duplicate session dates.")
    return ordered


def _month_end(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _dates(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _download(url: str) -> bytes:
    with urlopen(url, timeout=30) as response:  # noqa: S310 - injected only in tests
        return response.read()


class _InMemoryArchiveRevisionStore:
    """Default test/local implementation; production injects a durable repository."""

    def __init__(self) -> None:
        self._batches: dict[tuple[str, str], BtcusdtArchiveBatch] = {}

    def get(self, uri: str, sha256: str) -> BtcusdtArchiveBatch | None:
        return self._batches.get((uri, sha256))

    def put_if_absent(self, batch: BtcusdtArchiveBatch) -> BtcusdtArchiveBatch:
        if len(batch.source_artifacts) != 1:
            raise BinanceBtcusdtIngestionError(
                "One archive revision must have one source artifact."
            )
        artifact = batch.source_artifacts[0]
        return self._batches.setdefault((artifact.uri, artifact.sha256), batch)
