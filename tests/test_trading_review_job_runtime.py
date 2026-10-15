"""Normalization admission, production result serialization, and privacy checks."""

from datetime import UTC, datetime

from refinvest.app.trading_review_payload import build_normalization_payload
from refinvest.application.trading_review_jobs import normalization_request_fingerprint
from refinvest.trading_review import normalize_and_reconstruct
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    simple_long_request,
)


def test_admission_fingerprint_ignores_grant_refresh_and_object_order() -> None:
    first: dict[str, object] = {
        "coreLogicalJobId": "logical-1",
        "expectedInputSetHash": "a" * 64,
        "artifactAccessGrants": [
            {
                "role": "TRADE_HISTORY",
                "rawSha256": "b" * 64,
                "accessUri": "https://internal.example/one",
                "expiresAt": "2026-01-01T00:00:00Z",
            },
            {
                "role": "POSITION_HISTORY",
                "rawSha256": "c" * 64,
                "accessUri": "https://internal.example/two",
                "expiresAt": "2026-01-01T00:00:00Z",
            },
        ],
        "pinnedInstrumentTerms": [{"instrumentId": "b"}, {"instrumentId": "a"}],
    }
    second: dict[str, object] = {
        "pinnedInstrumentTerms": [{"instrumentId": "a"}, {"instrumentId": "b"}],
        "artifactAccessGrants": [
            {
                "accessUri": "https://internal.example/refreshed",
                "rawSha256": "c" * 64,
                "role": "POSITION_HISTORY",
                "expiresAt": "2026-01-01T00:01:00Z",
            },
            {
                "accessUri": "https://internal.example/other",
                "rawSha256": "b" * 64,
                "role": "TRADE_HISTORY",
                "expiresAt": "2026-01-01T00:01:00Z",
            },
        ],
        "expectedInputSetHash": "a" * 64,
        "coreLogicalJobId": "logical-1",
    }
    assert normalization_request_fingerprint(first) == normalization_request_fingerprint(second)
    second["expectedInputSetHash"] = "d" * 64
    assert normalization_request_fingerprint(first) != normalization_request_fingerprint(second)
    second["expectedInputSetHash"] = "a" * 64
    second["attemptToken"] = "new-attempt"
    assert normalization_request_fingerprint(first) != normalization_request_fingerprint(second)


def test_production_result_payload_is_complete_and_masks_raw_identifiers() -> None:
    request = simple_long_request()
    result = normalize_and_reconstruct(request)
    terminal_at = datetime(2026, 1, 3, tzinfo=UTC)
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
        created_at=terminal_at,
        terminal_at=terminal_at,
        instrument_terms=request.instruments,
    )
    assert payload["status"] == "COMPLETED"
    assert payload["inputSetHash"] == input_set_hash_for(identity)
    assert payload["reconciliationManifest"]["executionInputHash"] == result.input_hash
    assert payload["resultHash"] == result.result_hash
    assert payload["attemptToken"] == "attempt-1"
    assert payload["deletionGeneration"] == 0
    assert len(payload["artifactCoverages"]) == 2  # type: ignore[arg-type]
    assert len(payload["canonicalRecords"]) == len(result.canonical_records)  # type: ignore[arg-type]
    snapshots = payload["sourceEvidenceSnapshots"]
    assert isinstance(snapshots, list)
    assert snapshots
    trade = next(item for item in snapshots if item["artifactRole"] == "TRADE_HISTORY")
    assert trade["maskedTradeId"].startswith("tra_")
    assert trade["maskedOrderId"].startswith("ord_")
    assert "UID" not in {field["name"] for field in trade["allowlistedFields"]}
    assert "Trade ID" not in {field["name"] for field in trade["allowlistedFields"]}
    assert "Order ID" not in {field["name"] for field in trade["allowlistedFields"]}
    manifest = payload["reconciliationManifest"]
    assert isinstance(manifest, dict)
    units = manifest["reviewUnits"]
    assert isinstance(units, list)
    assert units[0]["initialNotional"]["amount"] == "20.00"
