"""Compare normalization routes and terminal JSON with checked-in OpenAPI 0.4.0."""

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from refinvest.api.routes.trading_review_jobs import _http_error
from refinvest.app.main import app
from refinvest.application.normalization_payload import build_normalization_payload
from refinvest.trading_review import normalize_and_reconstruct
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    simple_long_request,
)

CONTRACT = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "openapi/compute-api.yaml").read_text(encoding="utf-8")
)


def _schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": CONTRACT["components"]}
    )


def test_normalization_operation_ids_and_success_codes_match_contract() -> None:
    generated = app.openapi()["paths"]
    endpoints = (
        ("/trading-review/normalization-jobs", "post", 202),
        ("/trading-review/normalization-jobs/{jobId}/grant-updates", "post", 200),
        ("/trading-review/normalization-jobs/{jobId}", "get", 200),
        ("/trading-review/normalization-jobs/{jobId}/result", "get", 200),
        ("/trading-review/normalization-jobs/{jobId}/cancellation", "post", 202),
        ("/trading-review/terminal-payloads/{payloadId}", "get", 200),
        ("/trading-review/jobs/{jobId}/acknowledgement", "post", 204),
    )
    for path, method, success in endpoints:
        expected = CONTRACT["paths"][path][method]
        actual = generated[path][method]
        assert actual["operationId"] == expected["operationId"]
        assert str(success) in expected["responses"]
        assert str(success) in actual["responses"]


def test_terminal_payload_and_error_shape_validate_against_openapi() -> None:
    request = simple_long_request()
    result = normalize_and_reconstruct(request)
    now = datetime(2026, 1, 3, tzinfo=UTC)
    identity = normalization_identity_for(request)
    payload = build_normalization_payload(
        result,
        job_id="job-1",
        core_logical_job_id="logical-1",
        core_import_session_id="session-1",
        core_normalization_run_id="run-1",
        input_set_hash=input_set_hash_for(identity),
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        target_version_set=identity["targetVersionSet"],
        attempt_token="attempt-1",
        deletion_generation=0,
        artifacts=request.artifacts,
        masking_key=b"synthetic-test-masking-key-32chars",
        created_at=now,
        terminal_at=now,
        instrument_terms=request.instruments,
    )
    _schema("NormalizationTerminalPayload").validate(payload)
    v2 = build_normalization_payload(
        result,
        job_id="job-1",
        core_logical_job_id="logical-1",
        core_import_session_id="session-1",
        core_normalization_run_id="run-1",
        input_set_hash=input_set_hash_for(identity),
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        target_version_set=identity["targetVersionSet"],
        attempt_token="attempt-1",
        deletion_generation=0,
        artifacts=request.artifacts,
        masking_key=b"synthetic-test-masking-key-32chars",
        created_at=now,
        terminal_at=now,
        instrument_terms=request.instruments,
        result_schema_version="TRADING_NORMALIZATION_RESULT_V2",
        result_hash_specification_version="TRADING_NORMALIZATION_RESULT_HASH_V2",
    )
    _schema("NormalizationTerminalPayload").validate(v2)
    response = _http_error(409, "RESULT_NOT_AVAILABLE", "Result is not available.")
    _schema("ErrorResponse").validate(json.loads(response.body))
