"""Canonical, vendor-independent Daily Bar values."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True)
class AssetSession:
    asset: str
    session_date: date


class AssetCalendar(StrEnum):
    US_EQUITY = "US_EQUITY"
    CRYPTO_UTC = "CRYPTO_UTC"


def calendar_for_asset(asset: str) -> AssetCalendar:
    if asset in {"QQQ", "SPY", "TQQQ", "SOXL", "VIX"}:
        return AssetCalendar.US_EQUITY
    if asset == "BTCUSDT":
        return AssetCalendar.CRYPTO_UTC
    raise ValueError(f"Unsupported Asset calendar: {asset}")


class CalendarSessionState(StrEnum):
    EXPECTED = "EXPECTED"
    NON_TRADING = "NON_TRADING"


@dataclass(frozen=True)
class CalendarSession:
    state: CalendarSessionState
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state is CalendarSessionState.EXPECTED:
            if self.completed_at is None:
                raise ValueError("An expected AssetSession must include completed_at.")
            if self.completed_at.tzinfo is not UTC:
                raise ValueError("completed_at must be a timezone-aware UTC datetime.")
        elif self.completed_at is not None:
            raise ValueError("A non-trading AssetSession must not include completed_at.")


@dataclass(frozen=True)
class CompletedAssetSession:
    asset_session: AssetSession
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.completed_at.tzinfo is not UTC:
            raise ValueError("completed_at must be a timezone-aware UTC datetime.")


@dataclass(frozen=True)
class CalendarProvenance:
    source: str
    version: str
    content_hash: str


@dataclass(frozen=True)
class DailyBar:
    asset: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @property
    def asset_session(self) -> AssetSession:
        return AssetSession(self.asset, self.session_date)


class DailyBarAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    NON_TRADING_SESSION = "NON_TRADING_SESSION"
    MISSING_EXPECTED_SESSION = "MISSING_EXPECTED_SESSION"


@dataclass(frozen=True)
class DailyBarLookup:
    availability: DailyBarAvailability
    bar: DailyBar | None = None

    def __post_init__(self) -> None:
        if (self.availability is DailyBarAvailability.AVAILABLE) != (self.bar is not None):
            raise ValueError(
                "An available DailyBarLookup must contain a bar, and other states must not."
            )
