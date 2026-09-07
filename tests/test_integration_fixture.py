from datetime import date
from pathlib import Path

import pytest

from refinvest.app import integration_fixture
from refinvest.application.dataset_snapshots import DatasetSnapshot
from refinvest.ingestion.snapshot.artifacts import build_snapshot_artifact_contents


def test_deterministic_fixture_contains_exactly_the_five_mvp_assets() -> None:
    sessions = integration_fixture._sessions()
    bars = integration_fixture._bars()

    assert {asset_session.asset for asset_session in sessions} == {
        "QQQ",
        "SPY",
        "TQQQ",
        "SOXL",
        "BTCUSDT",
    }
    assert {bar.asset for bar in bars} == {
        "QQQ",
        "SPY",
        "TQQQ",
        "SOXL",
        "BTCUSDT",
    }
    assert {session.session_date for session in sessions if session.asset == "BTCUSDT"} == {
        date(2025, 1, day) for day in range(2, 9)
    }


def test_deterministic_fixture_builds_an_immutable_snapshot_without_vendor_inputs(
    tmp_path: Path,
) -> None:
    snapshot = DatasetSnapshot(
        integration_fixture.INTEGRATION_SNAPSHOT_ID,
        integration_fixture._CREATED_AT,
        "integration-fixture",
        {"firstDate": "2025-01-02", "lastDate": "2025-01-08"},
        "split-adjusted-ohlcv-cash-dividends-unadjusted",
        str(tmp_path / integration_fixture.INTEGRATION_SNAPSHOT_ID),
        integration_fixture._CALENDAR_PROVENANCE.source,
        integration_fixture._CALENDAR_PROVENANCE.version,
        integration_fixture._CALENDAR_PROVENANCE.content_hash,
    )

    contents = build_snapshot_artifact_contents(
        snapshot,
        integration_fixture._CALENDAR_PROVENANCE,
        integration_fixture._sessions(),
        integration_fixture._bars(),
        corporate_actions_applied=True,
        point_in_time_validation_passed=True,
    )

    assert b'"VIX"' not in contents.calendar
    assert b'"BTCUSDT"' in contents.daily_bars


def test_existing_fixture_without_all_mvp_assets_fails_fast() -> None:
    with pytest.raises(ValueError, match="outdated"):
        integration_fixture._assert_existing_fixture_has_mvp_coverage(
            _FixtureCalendar().expected_sessions
        )


class _FixtureCalendar:
    def expected_sessions(self, asset: str) -> tuple[date, ...]:
        return (date(2025, 1, 2),) if asset == "QQQ" else ()
