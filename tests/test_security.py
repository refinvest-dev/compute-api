from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from refinvest.app.config import Settings, get_settings
from refinvest.app.security import require_internal_api_key


def protected_test_app(settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_internal_api_key)])
    def protected() -> dict[str, str]:
        return {"status": "ok"}

    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_internal_api_key_rejects_missing_and_incorrect_values() -> None:
    client = TestClient(protected_test_app(Settings(internal_api_key="expected-key")))

    assert client.get("/protected").status_code == 401
    assert client.get("/protected", headers={"X-Internal-Api-Key": "wrong-key"}).status_code == 401


def test_internal_api_key_accepts_configured_value() -> None:
    client = TestClient(protected_test_app(Settings(internal_api_key="expected-key")))

    response = client.get("/protected", headers={"X-Internal-Api-Key": "expected-key"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_internal_api_key_reports_missing_configuration() -> None:
    client = TestClient(protected_test_app(Settings(internal_api_key=None)))

    assert client.get("/protected").status_code == 503
