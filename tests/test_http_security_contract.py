"""Authentication responses for every Core-facing OpenAPI operation."""

from __future__ import annotations

import json
import re
from importlib import import_module
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from starlette.testclient import TestClient

from refinvest.app.config import Settings, get_settings
from refinvest.app.main import create_app

CONTRACT = yaml.safe_load((Path(__file__).parents[1] / "openapi/compute-api.yaml").read_text())
OPERATIONS = [
    (method.upper(), path)
    for path, item in CONTRACT["paths"].items()
    for method, operation in item.items()
    if method in {"get", "post", "put", "patch", "delete"}
    and operation.get("security", CONTRACT["security"]) == [{"InternalApiKey": []}]
]
TRACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(("method", "path"), OPERATIONS)
@pytest.mark.parametrize(
    ("configured", "supplied", "status"),
    [("expected-key", None, 401), ("expected-key", "wrong-key", 401), (None, "any-key", 503)],
)
def test_contract_operations_preserve_authentication_responses(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    configured: str | None,
    supplied: str | None,
    status: int,
) -> None:
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings(configured)
    monkeypatch.setattr(import_module("refinvest.app.main"), "uuid4", lambda: UUID(TRACE_ID))
    uri = re.sub(r"\{([^}]+)\}", lambda match: "QQQ" if match[1] == "symbol" else TRACE_ID, path)
    headers = {} if supplied is None else {"X-Internal-Api-Key": supplied}
    with TestClient(application) as client:
        response = client.request(method, uri, headers=headers, json={})
    assert response.status_code == status
    if path.startswith("/trading-review/"):
        expected = {
            "code": "UNAUTHORIZED" if status == 401 else "TEMPORARY_DEPENDENCY_FAILURE",
            "message": "Internal request could not be completed.",
            "traceId": TRACE_ID,
            "retryable": status == 503,
        }
    else:
        expected = {
            "detail": "Invalid internal API key."
            if status == 401
            else "Internal API key is not configured."
        }
    assert response.content == json.dumps(expected, separators=(",", ":")).encode()
    assert response.headers["content-type"] == "application/json"
    assert "www-authenticate" not in response.headers


def test_every_runtime_contract_operation_is_represented() -> None:
    actual = {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
        for path, item in create_app().openapi()["paths"].items()
        if path != "/health"
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    # Placeholder names (run_id vs runId) do not change the HTTP resource path.
    expected = {(method, re.sub(r"\{[^}]+\}", "{}", path)) for method, path in OPERATIONS}
    assert actual == expected


@pytest.mark.parametrize("configured", [None, "expected-key"])
def test_deployment_health_remains_public(configured: str | None) -> None:
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings(configured)
    with TestClient(application) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.content == b'{"status":"ok"}'
