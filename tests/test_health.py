from fastapi.testclient import TestClient

from refinvest.app.main import app


def test_health_endpoint_is_available_without_internal_api_key() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
