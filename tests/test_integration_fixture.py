from datetime import date
from decimal import Decimal

from refinvest.app.integration_fixture import INTEGRATION_SNAPSHOT_ID, _bars, _sessions


def test_integration_fixture_is_a_fixed_qqq_snapshot() -> None:
    sessions = _sessions()
    bars = _bars()

    assert INTEGRATION_SNAPSHOT_ID == "integration-e2e-v1"
    assert tuple(session.session_date for session in sessions) == (
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 6),
        date(2025, 1, 7),
        date(2025, 1, 8),
    )
    assert [bar.close for bar in bars] == [
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
        Decimal("103"),
        Decimal("104"),
    ]
