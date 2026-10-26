"""Normalization admission, production result serialization, and privacy checks."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from refinvest.app.trading_review_payload import build_normalization_payload
from refinvest.app.trading_review_result_integrity import (
    ResultIntegrityError,
    canonicalize_result,
    verify_public_result,
)
from refinvest.application.trading_review_jobs import normalization_request_fingerprint
from refinvest.trading_review import normalize_and_reconstruct
from refinvest.trading_review.model import NormalizationRequest, NormalizationResult
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    position_row,
    request_for,
    simple_long_request,
)


def _v2_payload_for(
    request: NormalizationRequest, result: NormalizationResult | None = None
) -> dict[str, object]:
    identity = normalization_identity_for(request)
    now = datetime(2026, 1, 3, tzinfo=UTC)
    return build_normalization_payload(
        result if result is not None else normalize_and_reconstruct(request),
        job_id="job-v2",
        core_logical_job_id="logical-v2",
        core_import_session_id="session-v2",
        core_normalization_run_id="run-v2",
        attempt_token="attempt-v2",
        deletion_generation=0,
        input_set_hash=input_set_hash_for(identity),
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        target_version_set=identity["targetVersionSet"],
        artifacts=request.artifacts,
        masking_key=b"synthetic-test-masking-key-32chars",
        created_at=now,
        terminal_at=now,
        instrument_terms=request.instruments,
        result_schema_version="TRADING_NORMALIZATION_RESULT_V2",
        result_hash_specification_version="TRADING_NORMALIZATION_RESULT_HASH_V2",
    )


def test_v2_production_supplement_preserves_direct_evidence_and_real_zero() -> None:
    request = simple_long_request()
    result = normalize_and_reconstruct(request)
    payload = _v2_payload_for(request)
    supplement = payload["analyticsInputSupplement"]
    rows = supplement["reviewUnitEngineInputs"]
    assert len(rows) == payload["counts"]["reviewUnitCount"] == 1
    row = rows[0]
    episode = result.episodes[0]
    assert row["evidenceRecordIds"] == sorted(episode.evidence_record_ids)
    assert len(row["allocationEvidence"]) == payload["counts"]["positionAllocationCount"] == 2
    for allocation in episode.allocations:
        evidence = next(
            item for item in row["allocationEvidence"]
            if item["evidenceRecordIds"] == sorted(allocation.evidence_record_ids)
        )
        assert evidence["allocatedFee"]["sourceScale"] == allocation.allocated_fee.amount.scale
        assert evidence["allocatedReportedPnl"]["sourceScale"] == (
            allocation.allocated_reported_pnl.amount.scale
        )
    assert any(
        evidence["allocatedReportedPnl"]["amount"] == "0"
        for evidence in row["allocationEvidence"]
    )
    verify_public_result(payload)
    assert _v2_payload_for(request)["resultHash"] == payload["resultHash"]


def test_v2_excluded_shell_and_missing_direct_evidence() -> None:
    request = request_for([], [position_row("Long", "10", "11", "2", "2", "2")])
    payload = _v2_payload_for(request)
    row = payload["analyticsInputSupplement"]["reviewUnitEngineInputs"][0]
    assert payload["counts"]["excludedReviewUnitCount"] == 1
    assert row["allocationEvidence"] == []
    assert len(row["evidenceRecordIds"]) == 1
    result = normalize_and_reconstruct(request)
    broken = replace(
        result,
        episodes=(replace(result.episodes[0], evidence_record_ids=("record-missing",)),),
    )
    with pytest.raises(ResultIntegrityError, match="EVIDENCE_REFERENCE_INVALID"):
        _v2_payload_for(request, broken)


def test_v2_missing_allocated_money_fails_instead_of_becoming_zero() -> None:
    request = simple_long_request()
    result = normalize_and_reconstruct(request)
    episode = result.episodes[0]
    allocations = (
        replace(episode.allocations[0], allocated_fee=None),  # type: ignore[arg-type]
        *episode.allocations[1:],
    )
    broken = replace(result, episodes=(replace(episode, allocations=allocations),))
    with pytest.raises(ResultIntegrityError, match="RESULT_SCHEMA_UNSUPPORTED"):
        _v2_payload_for(request, broken)


def test_admission_fingerprint_ignores_grant_refresh_and_object_order() -> None:
    first: dict[str, object] = {
        "coreLogicalJobId": "logical-1",
        "expectedInputSetHash": "a" * 64,
        "resultSchemaVersion": "TRADING_NORMALIZATION_RESULT_V1",
        "resultHashSpecificationVersion": "TRADING_NORMALIZATION_RESULT_HASH_V1",
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
        "resultSchemaVersion": "TRADING_NORMALIZATION_RESULT_V1",
        "resultHashSpecificationVersion": "TRADING_NORMALIZATION_RESULT_HASH_V1",
    }
    assert normalization_request_fingerprint(first) == normalization_request_fingerprint(second)
    second["expectedInputSetHash"] = "d" * 64
    assert normalization_request_fingerprint(first) != normalization_request_fingerprint(second)
    second["expectedInputSetHash"] = "a" * 64
    second["attemptToken"] = "new-attempt"
    assert normalization_request_fingerprint(first) != normalization_request_fingerprint(second)
    second.pop("attemptToken")
    second["resultSchemaVersion"] = "TRADING_NORMALIZATION_RESULT_V2"
    second["resultHashSpecificationVersion"] = "TRADING_NORMALIZATION_RESULT_HASH_V2"
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
    assert (
        payload["resultHash"]
        == hashlib.sha256(canonicalize_result(payload["resultDigestManifest"])).hexdigest()
    )
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
    assert units[0]["initialNotional"]["amount"] == "20"
