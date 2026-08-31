"""Generate the fixed UTC calendar artifact for Binance Spot BTCUSDT daily bars."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
)

CRYPTO_UTC_ASSETS = ("BTCUSDT",)
CRYPTO_UTC_CALENDAR_SOURCE = "fixed:CRYPTO_UTC"
CRYPTO_UTC_CALENDAR_VERSION = "1"


class CryptoUtcCalendarIngestionError(ValueError):
    """A CRYPTO_UTC calendar cannot be generated as one complete immutable artifact."""


@dataclass(frozen=True)
class CryptoUtcCalendarArtifact:
    provenance: CalendarProvenance
    sessions: dict[AssetSession, CalendarSession]


def build_crypto_utc_calendar(start: date, end: date) -> CryptoUtcCalendarArtifact:
    """Build all BTCUSDT UTC dates as completed expected sessions for one snapshot."""
    if start > end:
        raise CryptoUtcCalendarIngestionError("Calendar start must not exceed end.")
    sessions = {
        AssetSession("BTCUSDT", session_date): CalendarSession(
            CalendarSessionState.EXPECTED,
            datetime.combine(session_date + timedelta(days=1), datetime.min.time(), UTC),
        )
        for session_date in _dates_inclusive(start, end)
    }
    return CryptoUtcCalendarArtifact(
        CalendarProvenance(
            CRYPTO_UTC_CALENDAR_SOURCE,
            CRYPTO_UTC_CALENDAR_VERSION,
            _calendar_rule_hash(),
        ),
        sessions,
    )


def _dates_inclusive(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _calendar_rule_hash() -> str:
    rule = {
        "assets": CRYPTO_UTC_ASSETS,
        "completedAt": "next-utc-midnight",
        "expectedEveryUtcDate": True,
    }
    canonical_rule = json.dumps(rule, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical_rule).hexdigest()
