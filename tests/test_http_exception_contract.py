"""Public error response bytes and exception dispatch before handler extraction."""

from __future__ import annotations

import json
from importlib import import_module
from uuid import UUID

import pytest
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from refinvest.app.config import Settings, get_settings
from refinvest.app.main import create_app

TRACE_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def handler_module(monkeypatch: pytest.MonkeyPatch):
    module = import_module("refinvest.api.exception_handlers")
    monkeypatch.setattr(module, "uuid4", lambda: UUID(TRACE_ID))
    return module


@pytest.mark.parametrize(
    "prefix",
    [
        "/backtests",
        "/series",
        "/trading-review/analytics-jobs",
        "/trading-review/normalization-jobs",
    ],
)
def test_validation_error_preserves_family_specific_body(handler_module, prefix: str) -> None:
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings(None)
    error = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "strategyVersion"),
                "msg": "Field required",
                "input": {"private": "not-public"},
            },
            {
                "type": "greater_than_equal",
                "loc": ("body", "strategyVersion", "conditions", 2, "window"),
                "msg": "Input should be greater than or equal to 1",
                "input": "not-public",
            },
        ],
        body={"private": "not-public"},
    )

    @application.get(prefix + "/handler-contract/error", include_in_schema=False)
    def fail() -> None:
        raise error

    with TestClient(application) as client:
        response = client.get(prefix + "/handler-contract/error")
    expected = (
        {
            "code": "REQUEST_INVALID",
            "message": "Trading Review request shape is invalid.",
            "traceId": TRACE_ID,
            "retryable": False,
        }
        if prefix.startswith("/trading-review/")
        else {
            "message": "Backtest request validation failed.",
            "errorCode": "DSL_INVALID",
            "details": [
                "body.strategyVersion: Field required",
                (
                    "body.strategyVersion.conditions.2.window: "
                    "Input should be greater than or equal to 1"
                ),
            ],
        }
    )
    assert response.status_code == 400
    assert response.content == json.dumps(expected, separators=(",", ":")).encode()
    assert response.headers["content-type"] == "application/json"
    assert b"not-public" not in response.content


@pytest.mark.parametrize("prefix", ["/backtests", "/trading-review/analytics-jobs"])
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "UNAUTHORIZED"),
        (404, "JOB_NOT_FOUND"),
        (503, "TEMPORARY_DEPENDENCY_FAILURE"),
        (409, "REQUEST_INVALID"),
        (422, "REQUEST_INVALID"),
        (500, "REQUEST_INVALID"),
    ],
)
def test_http_error_preserves_body_status_and_header_policy(
    handler_module, prefix: str, status: int, code: str
) -> None:
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: Settings(None)

    @application.get(prefix + "/handler-contract/error", include_in_schema=False)
    def fail() -> None:
        raise HTTPException(status, detail="synthetic-detail", headers={"Retry-After": "9"})

    with TestClient(application) as client:
        response = client.get(prefix + "/handler-contract/error")
    expected = (
        {
            "code": code,
            "message": "Internal request could not be completed.",
            "traceId": TRACE_ID,
            "retryable": status == 503,
        }
        if prefix.startswith("/trading-review/")
        else {"detail": "synthetic-detail"}
    )
    assert response.status_code == status
    assert response.content == json.dumps(expected, separators=(",", ":")).encode()
    assert response.headers["content-type"] == "application/json"
    assert "retry-after" not in response.headers
    if prefix.startswith("/trading-review/"):
        assert b"synthetic-detail" not in response.content


@pytest.mark.parametrize(
    "handler", ["request_validation_error_handler", "internal_http_error_handler"]
)
def test_unrelated_exceptions_are_reraised_unchanged(handler_module, handler: str) -> None:
    request = Request({"type": "http", "path": "/trading-review/analytics-jobs"})
    error = RuntimeError("synthetic-unexpected-error")
    with pytest.raises(RuntimeError) as caught:
        getattr(handler_module, handler)(request, error)
    assert caught.value is error
