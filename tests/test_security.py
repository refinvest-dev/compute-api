import pytest
from fastapi import HTTPException

from refinvest.app.config import Settings
from refinvest.app.security import require_internal_api_key


def test_internal_api_key_rejects_missing_and_incorrect_values() -> None:
    settings = Settings(internal_api_key="expected-key")

    with pytest.raises(HTTPException) as missing_key:
        require_internal_api_key(settings, None)
    with pytest.raises(HTTPException) as incorrect_key:
        require_internal_api_key(settings, "wrong-key")

    assert missing_key.value.status_code == 401
    assert incorrect_key.value.status_code == 401


def test_internal_api_key_accepts_configured_value() -> None:
    require_internal_api_key(Settings(internal_api_key="expected-key"), "expected-key")


def test_internal_api_key_reports_missing_configuration() -> None:
    with pytest.raises(HTTPException) as missing_configuration:
        require_internal_api_key(Settings(internal_api_key=None), None)

    assert missing_configuration.value.status_code == 503
