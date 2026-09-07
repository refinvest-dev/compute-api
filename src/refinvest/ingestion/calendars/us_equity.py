"""Generate immutable US-equity calendar artifacts from a pinned XNYS schedule."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from importlib.metadata import version
from typing import Any, Protocol, cast

from refinvest.marketdata.domain import (
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
)

US_EQUITY_ASSETS = ("QQQ", "SPY", "TQQQ", "SOXL")
US_EQUITY_CALENDAR_SOURCE = "exchange_calendars:XNYS+official-overrides"


class UsEquityCalendarIngestionError(ValueError):
    """A US-equity calendar cannot be generated as one complete immutable artifact."""


class SessionSchedule(Protocol):
    def sessions_in_range(self, start: date, end: date) -> Sequence[object]: ...

    def session_close(self, session: object) -> object: ...


@dataclass(frozen=True)
class UsEquityCalendarArtifact:
    provenance: CalendarProvenance
    sessions: dict[AssetSession, CalendarSession]


def generate_us_equity_calendar(
    start: date,
    end: date,
    official_overrides: Mapping[date, datetime | None],
    *,
    assets: tuple[str, ...] = US_EQUITY_ASSETS,
) -> UsEquityCalendarArtifact:
    """Use the locked exchange-calendars XNYS schedule for one ingestion-time artifact."""
    if start > end:
        raise UsEquityCalendarIngestionError("Calendar start must not exceed end.")
    try:
        package: Any = import_module("exchange_calendars")
        schedule = cast(SessionSchedule, package.get_calendar("XNYS"))
        package_version = version("exchange-calendars")
    except (ImportError, ValueError) as error:
        raise UsEquityCalendarIngestionError(
            "Pinned exchange-calendars XNYS is unavailable."
        ) from error
    return build_us_equity_calendar(
        start,
        end,
        official_overrides,
        schedule=schedule,
        package_version=package_version,
        assets=assets,
    )


def build_us_equity_calendar(
    start: date,
    end: date,
    official_overrides: Mapping[date, datetime | None],
    *,
    schedule: SessionSchedule,
    package_version: str,
    assets: tuple[str, ...] = US_EQUITY_ASSETS,
) -> UsEquityCalendarArtifact:
    """Build one deterministic artifact from a schedule plus official closure overrides."""
    if start > end:
        raise UsEquityCalendarIngestionError("Calendar start must not exceed end.")
    if not assets or any(asset not in US_EQUITY_ASSETS for asset in assets):
        raise UsEquityCalendarIngestionError(
            "US_EQUITY calendar assets must be supported MVP assets."
        )
    _validate_overrides(start, end, official_overrides)
    expected = {
        _session_date(session): _utc_datetime(schedule.session_close(session))
        for session in schedule.sessions_in_range(start, end)
    }
    expected.update(
        {
            override_date: completed_at
            for override_date, completed_at in official_overrides.items()
            if completed_at is not None
        }
    )
    for override_date, completed_at in official_overrides.items():
        if completed_at is None:
            expected.pop(override_date, None)
    provenance = CalendarProvenance(
        US_EQUITY_CALENDAR_SOURCE,
        package_version,
        _override_hash(official_overrides),
    )
    sessions: dict[AssetSession, CalendarSession] = {}
    for session_date in _dates_inclusive(start, end):
        state = (
            CalendarSessionState.EXPECTED
            if session_date in expected
            else CalendarSessionState.NON_TRADING
        )
        for asset in assets:
            sessions[AssetSession(asset, session_date)] = CalendarSession(
                state, expected.get(session_date)
            )
    return UsEquityCalendarArtifact(provenance, sessions)


def _validate_overrides(start: date, end: date, overrides: Mapping[date, datetime | None]) -> None:
    for session_date, completed_at in overrides.items():
        if not start <= session_date <= end:
            raise UsEquityCalendarIngestionError("Official calendar override is outside coverage.")
        if completed_at is not None and completed_at.tzinfo is not UTC:
            raise UsEquityCalendarIngestionError(
                "Official calendar override must be an exact UTC datetime."
            )


def _session_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    converted = getattr(value, "date", None)
    if not callable(converted):
        raise UsEquityCalendarIngestionError("XNYS session must expose a date.")
    result = converted()
    if not isinstance(result, date):
        raise UsEquityCalendarIngestionError("XNYS session date is invalid.")
    return result


def _utc_datetime(value: object) -> datetime:
    converted = getattr(value, "to_pydatetime", None)
    result = converted() if callable(converted) else value
    if (
        not isinstance(result, datetime)
        or result.tzinfo is None
        or result.utcoffset() != timedelta(0)
    ):
        raise UsEquityCalendarIngestionError("XNYS session close must be an exact UTC datetime.")
    return result.astimezone(UTC)


def _dates_inclusive(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _override_hash(overrides: Mapping[date, datetime | None]) -> str:
    payload = [
        {
            "date": session_date.isoformat(),
            "completedAt": completed_at.isoformat() if completed_at else None,
        }
        for session_date, completed_at in sorted(overrides.items())
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
