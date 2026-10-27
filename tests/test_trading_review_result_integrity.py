"""ADR-066 public result oracle and integrity regression tests."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from refinvest.app.trading_review_payload import build_normalization_payload
from refinvest.app.trading_review_result_integrity import (
    RESULT_HASH_SPECIFICATION_V1,
    ResultIntegrityError,
    canonicalize_result,
    finalize_public_result,
    parse_result_json_bytes,
    semantic_section_values,
    verify_public_result,
)
from refinvest.trading_review import normalize_and_reconstruct
from tests.trading_review_support import (
    input_set_hash_for,
    normalization_identity_for,
    simple_long_request,
)

VECTOR = json.loads(
    (
        Path(__file__).parent
        / "fixtures/trading_review/normalization_identity/v1/result-integrity-v1.json"
    ).read_text(encoding="utf-8")
)
V2_VECTOR = json.loads(
    (
        Path(__file__).parent
        / "fixtures/trading_review/normalization_identity/v2/result-integrity-v2.json"
    ).read_text(encoding="utf-8")
)


def _payload() -> dict[str, object]:
    return copy.deepcopy(VECTOR["terminalPayload"])


def _v2_payload() -> dict[str, object]:
    return copy.deepcopy(V2_VECTOR["terminalPayload"])


def test_v2_fixed_vector_matches_all_section_manifest_and_result_hashes() -> None:
    payload = finalize_public_result(_v2_payload())
    sections = semantic_section_values(payload)
    assert len(payload["sectionDigests"]) == 10
    expected_digests = V2_VECTOR["terminalPayload"]["sectionDigests"]
    assert payload["sectionDigests"] == expected_digests
    for digest in payload["sectionDigests"]:
        name = digest["sectionType"]
        expected = V2_VECTOR["sectionVectors"][name]
        assert canonicalize_result(sections[name]) == expected["canonicalJson"].encode("utf-8")
        assert digest["sectionHash"] == expected["expectedSha256"]
    assert expected_digests[0]["sectionType"] == "ANALYTICS_INPUT_SUPPLEMENT"
    assert expected_digests[0]["itemCount"] == 1
    assert canonicalize_result(sections["RECONCILIATION_MANIFEST"]) == (
        V2_VECTOR["manifestVector"]["canonicalJson"].encode("utf-8")
    )
    assert payload["reconciliationManifest"]["manifestHash"] == (
        V2_VECTOR["manifestVector"]["expectedSha256"]
    )
    assert canonicalize_result(payload["resultDigestManifest"]) == (
        V2_VECTOR["resultDigestManifestVector"]["canonicalJson"].encode("utf-8")
    )
    assert payload["resultHash"] == V2_VECTOR["resultDigestManifestVector"]["expectedSha256"]
    verify_public_result(payload)


@pytest.mark.parametrize("case", V2_VECTOR["invalidCases"], ids=lambda item: item["id"])
def test_v2_invalid_reference_and_money_cases_fail_with_contract_code(
    case: dict[str, object],
) -> None:
    payload = _v2_payload()
    mutation = case["mutation"]
    path = str(mutation["path"]).split("/")[2:]
    node: object = payload
    for part in path[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    assert isinstance(node, (dict, list))
    key = path[-1]
    if "removeIndex" in mutation:
        node[key].pop(mutation["removeIndex"])
    elif isinstance(node, list):
        node[int(key)] = mutation["value"]
    else:
        node[key] = mutation["value"]
    with pytest.raises(ResultIntegrityError, match=str(case["expectedCode"])):
        verify_public_result(payload)


def test_v2_pair_and_evidence_duplicates_are_rejected_without_v1_reinterpretation() -> None:
    for schema, specification in (
        ("TRADING_NORMALIZATION_RESULT_V1", "TRADING_NORMALIZATION_RESULT_HASH_V2"),
        ("TRADING_NORMALIZATION_RESULT_V2", "TRADING_NORMALIZATION_RESULT_HASH_V1"),
    ):
        payload = _v2_payload()
        payload["resultSchemaVersion"] = schema
        payload["resultHashSpecificationVersion"] = specification
        with pytest.raises(ResultIntegrityError, match="RESULT_SCHEMA_UNSUPPORTED"):
            finalize_public_result(payload)
    duplicate = _v2_payload()
    row = duplicate["analyticsInputSupplement"]["reviewUnitEngineInputs"][0]
    row["evidenceRecordIds"].append(row["evidenceRecordIds"][0])
    with pytest.raises(ResultIntegrityError, match="RESULT_ITEM_CONFLICT"):
        finalize_public_result(duplicate)
    v1_with_supplement = _payload()
    v1_with_supplement["analyticsInputSupplement"] = _v2_payload()["analyticsInputSupplement"]
    with pytest.raises(ResultIntegrityError, match="RESULT_SCHEMA_UNSUPPORTED"):
        finalize_public_result(v1_with_supplement)


def test_v2_unit_allocation_identity_and_order_are_checked_before_hashing() -> None:
    duplicated_unit = _v2_payload()
    unit = duplicated_unit["reconciliationManifest"]["reviewUnits"][0]
    duplicated_unit["reconciliationManifest"]["reviewUnits"].append(copy.deepcopy(unit))
    with pytest.raises(ResultIntegrityError, match="RESULT_ITEM_CONFLICT"):
        finalize_public_result(duplicated_unit)

    duplicated_allocation = _v2_payload()
    allocations = duplicated_allocation["reconciliationManifest"]["reviewUnits"][0][
        "allocations"
    ]
    allocations.append(copy.deepcopy(allocations[0]))
    with pytest.raises(ResultIntegrityError, match="RESULT_ITEM_CONFLICT"):
        finalize_public_result(duplicated_allocation)

    unsorted = _v2_payload()
    evidence = unsorted["analyticsInputSupplement"]["reviewUnitEngineInputs"][0][
        "evidenceRecordIds"
    ]
    evidence.reverse()
    with pytest.raises(ResultIntegrityError, match="RESULT_SCHEMA_UNSUPPORTED"):
        finalize_public_result(unsorted)

    missing = _v2_payload()
    missing.pop("analyticsInputSupplement")
    with pytest.raises(ResultIntegrityError, match="RESULT_SCHEMA_UNSUPPORTED"):
        finalize_public_result(missing)


def test_context_fixed_result_vector_at_every_canonical_boundary() -> None:
    payload = finalize_public_result(_payload())
    assert payload["resultHashSpecificationVersion"] == RESULT_HASH_SPECIFICATION_V1
    assert payload["counts"] == VECTOR["terminalPayload"]["counts"]
    sections = semantic_section_values(payload)
    digests = {item["sectionType"]: item for item in payload["sectionDigests"]}
    for name, expected in VECTOR["sectionVectors"].items():
        canonical = canonicalize_result(sections[name])
        assert canonical == expected["canonicalJson"].encode("utf-8")
        assert canonical.hex() == expected["canonicalUtf8Hex"]
        assert hashlib.sha256(canonical).hexdigest() == expected["expectedSha256"]
        assert digests[name]["sectionHash"] == expected["expectedSha256"]
        assert digests[name]["itemCount"] == (
            1 if name in {"QUALITY_REPORT", "RECONCILIATION_MANIFEST"} else len(sections[name])
        )
    manifest = payload["reconciliationManifest"]
    assert manifest["manifestHash"] == VECTOR["manifestVector"]["expectedSha256"]
    assert canonicalize_result(sections["RECONCILIATION_MANIFEST"]) == (
        VECTOR["manifestVector"]["canonicalJson"].encode("utf-8")
    )
    digest_bytes = canonicalize_result(payload["resultDigestManifest"])
    assert digest_bytes == VECTOR["resultDigestManifestVector"]["canonicalJson"].encode("utf-8")
    assert digest_bytes.hex() == VECTOR["resultDigestManifestVector"]["canonicalUtf8Hex"]
    assert payload["resultHash"] == VECTOR["resultDigestManifestVector"]["expectedSha256"]
    verify_public_result(payload)


def test_order_and_retrieval_duplicates_do_not_change_semantic_result() -> None:
    first = finalize_public_result(_payload())
    reordered = _payload()
    for key in ("artifactCoverages", "canonicalRecords", "sourceEvidenceSnapshots", "capability"):
        reordered[key].reverse()
    manifest = reordered["reconciliationManifest"]
    for key in (
        "artifactCoverages",
        "reviewUnits",
        "reconciliationMatches",
        "reconciliationIssues",
        "capability",
    ):
        manifest[key].reverse()
    reordered["canonicalRecords"].append(copy.deepcopy(reordered["canonicalRecords"][0]))
    reordered["sourceEvidenceSnapshots"].append(
        copy.deepcopy(reordered["sourceEvidenceSnapshots"][0])
    )
    second = finalize_public_result(reordered)
    assert second["counts"] == first["counts"]
    assert second["sectionDigests"] == first["sectionDigests"]
    assert second["resultHash"] == first["resultHash"]


def test_semantic_hash_is_independent_of_logical_page_size_and_arrival_order() -> None:
    request = simple_long_request()
    identity = normalization_identity_for(request)
    now = datetime(2026, 1, 3, tzinfo=UTC)
    source = build_normalization_payload(
        normalize_and_reconstruct(request),
        job_id="job",
        core_logical_job_id="logical",
        core_import_session_id="session",
        core_normalization_run_id="run",
        attempt_token="attempt",
        deletion_generation=0,
        input_set_hash=input_set_hash_for(identity),
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        target_version_set=identity["targetVersionSet"],
        artifacts=request.artifacts,
        masking_key=b"synthetic-test-masking-key-32chars",
        created_at=now,
        terminal_at=now,
        instrument_terms=request.instruments,
    )
    assert len(source["canonicalRecords"]) > 1
    expected = finalize_public_result(source)["resultHash"]
    for page_size in (1, 99):
        candidate = copy.deepcopy(source)
        records = candidate["canonicalRecords"]
        pages = [records[index : index + page_size] for index in range(0, len(records), page_size)]
        candidate["canonicalRecords"] = [item for page in reversed(pages) for item in page]
        assert finalize_public_result(candidate)["resultHash"] == expected


def test_same_identity_with_different_public_payload_fails() -> None:
    payload = _payload()
    conflict = copy.deepcopy(payload["canonicalRecords"][0])
    conflict["side"] = "SELL"
    payload["canonicalRecords"].append(conflict)
    with pytest.raises(ResultIntegrityError, match="RESULT_ITEM_CONFLICT"):
        finalize_public_result(payload)


def test_missing_identity_and_invalid_reference_fail() -> None:
    missing = _payload()
    missing["canonicalRecords"][0].pop("recordId")
    with pytest.raises(ResultIntegrityError, match="RESULT_ITEM_CONFLICT"):
        finalize_public_result(missing)
    dangling = _payload()
    dangling["canonicalRecords"][0]["sourceEvidenceSnapshotId"] = "missing"
    with pytest.raises(ResultIntegrityError, match="EVIDENCE_REFERENCE_INVALID"):
        finalize_public_result(dangling)


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("counts", "canonicalRecordCount"), 2, "RESULT_COUNT_MISMATCH"),
        (("sectionDigests", 0, "sectionHash"), "0" * 64, "RESULT_SECTION_HASH_MISMATCH"),
        (
            ("reconciliationManifest", "manifestHash"),
            "0" * 64,
            "RECONCILIATION_MANIFEST_HASH_MISMATCH",
        ),
        (("resultHash",), "0" * 64, "RESULT_HASH_MISMATCH"),
        (("resultSchemaVersion",), "FUTURE", "RESULT_SCHEMA_UNSUPPORTED"),
    ],
)
def test_declared_integrity_tampering_has_specific_failure(
    path: tuple[str | int, ...], value: object, code: str
) -> None:
    payload = _payload()
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ResultIntegrityError, match=code):
        verify_public_result(payload)


def test_transport_bytes_hash_is_separate_from_semantic_hash() -> None:
    payload = finalize_public_result(_payload())
    body = canonicalize_result(payload)
    transport_hash = hashlib.sha256(body).hexdigest()
    assert transport_hash != payload["resultHash"]
    assert hashlib.sha256(body + b" ").hexdigest() != transport_hash
    changed_runtime = _payload()
    changed_runtime["attemptToken"] = "another-attempt"
    changed = finalize_public_result(changed_runtime)
    assert changed["resultHash"] == payload["resultHash"]
    assert hashlib.sha256(canonicalize_result(changed)).hexdigest() != transport_hash


def test_decimal_timestamp_unicode_and_empty_section_canonicalization() -> None:
    assert canonicalize_result({"😀": 1, "\ue000": 2}) == '{"😀":1,"\ue000":2}'.encode()
    payload = _payload()
    payload["canonicalRecords"][0]["price"]["value"] = "001.2300"
    payload["canonicalRecords"][0]["occurredAt"] = "2025-01-01T09:00:00+09:00"
    finalized = finalize_public_result(payload)
    assert finalized["canonicalRecords"][0]["price"]["value"] == "1.23"
    assert finalized["canonicalRecords"][0]["occurredAt"] == "2025-01-01T00:00:00Z"
    empty_hash = hashlib.sha256(b"[]").hexdigest()
    digests = {item["sectionType"]: item for item in finalized["sectionDigests"]}
    assert digests["RECONCILIATION_MATCHES"]["sectionHash"] == empty_hash


@pytest.mark.parametrize(
    "payload",
    [b'{"a":1,"a":1}', b"\xef\xbb\xbf{}", b'{"amount":1.2}', b'{"a":NaN}'],
)
def test_ambiguous_json_resource_is_rejected(payload: bytes) -> None:
    with pytest.raises(ResultIntegrityError, match="RESULT_SCHEMA_UNSUPPORTED"):
        parse_result_json_bytes(payload)
