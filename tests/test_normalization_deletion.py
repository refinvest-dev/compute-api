"""TR-ACC-DELETE-009/011/013: truthful absence and exact identity transport."""

from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from refinvest.api.routes.trading_normalization_deletion import deletion_repository
from refinvest.app.config import Settings, get_settings
from refinvest.app.main import app
from refinvest.application.normalization_deletion import (
    DeleteNormalizationRuntime,
    NormalizationRuntimeAbsence,
)
from refinvest.application.trading_review_jobs import TradingReviewJobStateConflict
from tests.test_trading_review_routes import _validate


@pytest.mark.parametrize("absent", [False, True])
def test_pending_is_not_absence_and_all_correlation_fields_survive(absent):
    calls = []

    class Store:
        def purge(self, command):
            calls.append(command)
            return NormalizationRuntimeAbsence(command, datetime.now(UTC) if absent else None)

    app.dependency_overrides[get_settings] = lambda: Settings(internal_api_key="test-key")
    app.dependency_overrides[deletion_repository] = Store
    try:
        with TestClient(app) as client:
            response = client.put(
                "/trading-review/normalization-runs/run-1/deletion",
                headers={"X-Internal-Api-Key": "test-key"},
                json={
                    "coreImportSessionId": "session-1",
                    "sourceDeletionGeneration": 7,
                    "deletionRequestId": "request-1",
                    "deletionGeneration": 2,
                },
            )
        assert response.status_code == 200
        body = response.json()
        _validate("TradingNormalizationRuntimeDeletionResponse", body)
        assert body["state"] == ("ABSENT" if absent else "PENDING")
        assert (body["verifiedAbsentAt"] is not None) == absent
        assert body["retryAfterSeconds"] == (None if absent else 5)
        assert calls == [DeleteNormalizationRuntime("run-1", "session-1", 7, "request-1", 2)]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "failure,code", [(TradingReviewJobStateConflict("safe"), 409), (RuntimeError("private"), 503)]
)
def test_identity_conflict_and_dependency_failure_have_distinct_safe_results(failure, code):
    class Store:
        def purge(self, command):
            raise failure

    app.dependency_overrides[get_settings] = lambda: Settings(internal_api_key="test-key")
    app.dependency_overrides[deletion_repository] = Store
    try:
        with TestClient(app) as client:
            response = client.put(
                "/trading-review/normalization-runs/run-1/deletion",
                headers={"X-Internal-Api-Key": "test-key"},
                json={
                    "coreImportSessionId": "session-1",
                    "sourceDeletionGeneration": 0,
                    "deletionRequestId": "request-1",
                    "deletionGeneration": 1,
                },
            )
        assert response.status_code == code
        assert "private" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_unauthorized_and_malformed_requests_cannot_reach_cleanup():
    class Store:
        def purge(self, command):
            raise AssertionError("Cleanup must not run")

    app.dependency_overrides[get_settings] = lambda: Settings(internal_api_key="test-key")
    app.dependency_overrides[deletion_repository] = Store
    try:
        with TestClient(app) as client:
            path = "/trading-review/normalization-runs/run-1/deletion"
            assert client.put(path, json={}).status_code == 401
            invalid = {
                "coreImportSessionId": "s",
                "sourceDeletionGeneration": 0,
                "deletionRequestId": "r",
                "deletionGeneration": True,
            }
            assert (
                client.put(
                    path, headers={"X-Internal-Api-Key": "test-key"}, json=invalid
                ).status_code
                == 400
            )
            duplicate = '{"coreImportSessionId":"s","coreImportSessionId":"other"}'
            assert (
                client.put(
                    path,
                    headers={"X-Internal-Api-Key": "test-key", "Content-Type": "application/json"},
                    content=duplicate,
                ).status_code
                == 422
            )
    finally:
        app.dependency_overrides.clear()
