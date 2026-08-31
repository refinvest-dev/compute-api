from datetime import UTC, date, datetime

import pytest

from refinvest.ingestion.calendars.crypto_utc import (
    CRYPTO_UTC_CALENDAR_SOURCE,
    CRYPTO_UTC_CALENDAR_VERSION,
    CryptoUtcCalendarIngestionError,
    build_crypto_utc_calendar,
)
from refinvest.marketdata.domain import AssetSession, CalendarSessionState


def test_builds_every_utc_date_as_a_completed_expected_btcusdt_session() -> None:
    artifact = build_crypto_utc_calendar(date(2025, 1, 3), date(2025, 1, 5))

    assert artifact.provenance.source == CRYPTO_UTC_CALENDAR_SOURCE
    assert artifact.provenance.version == CRYPTO_UTC_CALENDAR_VERSION
    assert set(artifact.sessions) == {
        AssetSession("BTCUSDT", date(2025, 1, 3)),
        AssetSession("BTCUSDT", date(2025, 1, 4)),
        AssetSession("BTCUSDT", date(2025, 1, 5)),
    }
    assert all(
        session.state is CalendarSessionState.EXPECTED for session in artifact.sessions.values()
    )
    assert artifact.sessions[AssetSession("BTCUSDT", date(2025, 1, 3))].completed_at == datetime(
        2025, 1, 4, tzinfo=UTC
    )
    assert artifact.sessions[AssetSession("BTCUSDT", date(2025, 1, 5))].completed_at == datetime(
        2025, 1, 6, tzinfo=UTC
    )


def test_calendar_provenance_is_deterministic() -> None:
    first = build_crypto_utc_calendar(date(2025, 1, 3), date(2025, 1, 5))
    second = build_crypto_utc_calendar(date(2025, 1, 3), date(2025, 1, 5))

    assert first.provenance == second.provenance


def test_rejects_an_inverted_coverage_range() -> None:
    with pytest.raises(CryptoUtcCalendarIngestionError, match="start must not exceed"):
        build_crypto_utc_calendar(date(2025, 1, 5), date(2025, 1, 3))
