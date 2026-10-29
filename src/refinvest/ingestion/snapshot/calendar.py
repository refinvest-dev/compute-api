"""Canonical composite calendar provenance for immutable snapshot artifacts."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from refinvest.marketdata.domain import (
    AssetCalendar,
    AssetSession,
    CalendarProvenance,
    CalendarSession,
    CalendarSessionState,
    calendar_for_asset,
)

COMPOSITE_CALENDAR_SOURCE = "composite-calendar"
COMPOSITE_CALENDAR_VERSION = "1"


@dataclass(frozen=True)
class CalendarComponent:
    """One calendar's asset ownership and immutable source provenance."""

    calendar: AssetCalendar
    assets: tuple[str, ...]
    provenance: CalendarProvenance


def composite_calendar_provenance(
    components: Sequence[CalendarComponent],
    sessions: Mapping[AssetSession, CalendarSession],
) -> CalendarProvenance:
    """Return the deterministic top-level provenance for a complete composite calendar."""
    _validate_components(components, sessions)
    content = _canonical_json(
        {
            "components": _component_records(components),
            "sessions": calendar_session_records(sessions),
        }
    )
    return CalendarProvenance(
        COMPOSITE_CALENDAR_SOURCE,
        COMPOSITE_CALENDAR_VERSION,
        hashlib.sha256(content).hexdigest(),
    )


def validate_composite_calendar(
    provenance: CalendarProvenance,
    components: Sequence[CalendarComponent],
    sessions: Mapping[AssetSession, CalendarSession],
) -> None:
    """Reject a composite artifact whose claimed identity or ownership is ambiguous."""
    expected = composite_calendar_provenance(components, sessions)
    if provenance != expected:
        raise ValueError("Composite calendar provenance does not match canonical content.")


def calendar_session_records(
    sessions: Mapping[AssetSession, CalendarSession],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for asset_session, session in sorted(
        sessions.items(), key=lambda item: (item[0].asset, item[0].session_date)
    ):
        record: dict[str, object] = {
            "asset": asset_session.asset,
            "sessionDate": asset_session.session_date.isoformat(),
            "state": session.state.value,
        }
        if session.state is CalendarSessionState.EXPECTED:
            assert session.completed_at is not None
            record["completedAt"] = session.completed_at.isoformat()
        records.append(record)
    return records


def _validate_components(
    components: Sequence[CalendarComponent],
    sessions: Mapping[AssetSession, CalendarSession],
) -> None:
    if len(components) < 2:
        raise ValueError("A composite calendar requires at least two components.")
    calendars = [component.calendar for component in components]
    if len(set(calendars)) != len(calendars):
        raise ValueError("Composite calendar components must have distinct calendars.")
    owners: dict[str, AssetCalendar] = {}
    for component in components:
        if not component.assets:
            raise ValueError("A composite calendar component must own at least one asset.")
        for asset in component.assets:
            if asset in owners:
                raise ValueError("A composite calendar asset must have exactly one owner.")
            if calendar_for_asset(asset) is not component.calendar:
                raise ValueError("Composite calendar asset does not match its calendar.")
            owners[asset] = component.calendar
    session_assets = {asset_session.asset for asset_session in sessions}
    if session_assets != owners.keys():
        raise ValueError("Composite calendar components must own every and only session asset.")


def _component_records(components: Sequence[CalendarComponent]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for component in components:
        records.append(
            {
                "calendar": component.calendar.value,
                "assets": sorted(component.assets),
                "provenance": {
                    "source": component.provenance.source,
                    "version": component.provenance.version,
                    "contentHash": component.provenance.content_hash,
                },
            }
        )
    return sorted(records, key=lambda item: str(item["calendar"]))


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
