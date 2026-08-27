"""Canonical, vendor-independent Daily Bar values."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True)
class AssetSession:
    asset: str
    session_date: date


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
