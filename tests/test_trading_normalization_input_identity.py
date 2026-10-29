"""ADR-065 V1 fixed-vector and strict Core wire request checks."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from refinvest.api.schemas.trading_review_jobs import CreateTradingNormalizationJobTransport
from refinvest.contracts.normalization.identity import (
    NormalizationInputIdentity,
    calculate_input_set_hash,
    canonicalize_input_identity,
)

FIXTURES = Path(__file__).parent / "fixtures/trading_review/normalization_identity/v1"
VECTOR = json.loads((FIXTURES / "input-identity-v1.json").read_text(encoding="utf-8"))
WIRE = json.loads((FIXTURES / "wire-v1.json").read_text(encoding="utf-8"))["request"]
WIRE_CASES = json.loads((FIXTURES / "wire-v1.json").read_text(encoding="utf-8"))
OPENAPI = yaml.safe_load(
    (Path(__file__).parents[1] / "openapi/compute-api.yaml").read_text(encoding="utf-8")
)


def _validate_schema(name: str, document: dict[str, object]) -> None:
    Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": OPENAPI["components"]},
        format_checker=FormatChecker(),
    ).validate(document)


def test_context_wire_fixtures_validate_against_compute_openapi() -> None:
    _validate_schema("CreateTradingNormalizationJobRequest", WIRE_CASES["request"])
    _validate_schema("NormalizationTerminalResultDescriptor", WIRE_CASES["completedDescriptor"])
    _validate_schema("TradingNormalizationJobStatusResponse", WIRE_CASES["failedStatus"])


def test_context_fixed_vector_bytes_and_hash() -> None:
    identity = NormalizationInputIdentity.model_validate(VECTOR["inputIdentity"])
    canonical = canonicalize_input_identity(identity)
    assert canonical == VECTOR["canonicalJson"].encode("utf-8")
    assert canonical.hex() == VECTOR["canonicalUtf8Hex"]
    assert (
        calculate_input_set_hash(identity)
        == "1122481b2c208afc1cae751f952f1b9a670f446f404d42c1e581a3b83f25552e"
    )
    assert not canonical.startswith(b"\xef\xbb\xbf")
    assert not canonical.endswith(b"\n")


def test_core_wire_request_round_trip() -> None:
    request = CreateTradingNormalizationJobTransport.model_validate(WIRE)
    assert request.core_normalization_run_id == "run-example"
    assert request.retry_policy_version == "POLICY_V1"
    assert request.model_dump(mode="json", by_alias=True) == WIRE
    assert (
        request.input_identity.target_version_set.reconstruction.instrument_terms_version_by_instrument
        == {"instrument-example": "TERMS_V1"}
    )


@pytest.mark.parametrize(
    "path",
    [
        ("coreNormalizationRunId",),
        ("retryPolicyVersion",),
        ("inputIdentity", "tradingBookId"),
        ("inputIdentity", "targetVersionSet"),
        ("inputIdentity", "targetVersionSet", "normalizer"),
        ("inputIdentity", "artifactsByRole", "POSITION_HISTORY"),
    ],
)
def test_missing_required_field_rejected(path: tuple[str, ...]) -> None:
    body = copy.deepcopy(WIRE)
    node = body
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(ValidationError):
        CreateTradingNormalizationJobTransport.model_validate(body)


def test_unknown_fields_and_wrong_casing_rejected() -> None:
    for body in (
        dict(WIRE, unexpected=True),
        dict(WIRE, core_normalization_run_id=WIRE["coreNormalizationRunId"]),
    ):
        with pytest.raises(ValidationError):
            CreateTradingNormalizationJobTransport.model_validate(body)


def test_bad_hash_duplicate_role_and_locator_in_identity_rejected() -> None:
    bad_hash = copy.deepcopy(WIRE)
    bad_hash["inputIdentity"]["artifactsByRole"]["TRADE_HISTORY"]["sha256"] = "A" * 64
    duplicate_grants = copy.deepcopy(WIRE)
    duplicate_grants["artifactAccessGrants"][1]["role"] = "TRADE_HISTORY"
    locator = copy.deepcopy(WIRE)
    locator["inputIdentity"]["artifactsByRole"]["TRADE_HISTORY"]["accessUri"] = (
        "https://example.invalid"
    )
    for body in (bad_hash, duplicate_grants, locator):
        with pytest.raises(ValidationError):
            CreateTradingNormalizationJobTransport.model_validate(body)


def test_transport_grant_refresh_does_not_change_identity_hash() -> None:
    request = copy.deepcopy(WIRE)
    baseline = calculate_input_set_hash(
        NormalizationInputIdentity.model_validate(request["inputIdentity"])
    )
    request["artifactAccessGrants"][0]["accessUri"] = "https://example.invalid/refresh"
    request["artifactAccessGrants"][0]["expiresAt"] = "2026-01-01T00:04:00Z"
    assert (
        calculate_input_set_hash(
            NormalizationInputIdentity.model_validate(request["inputIdentity"])
        )
        == baseline
    )
    request["inputIdentity"]["artifactsByRole"]["TRADE_HISTORY"]["sha256"] = "c" * 64
    assert (
        calculate_input_set_hash(
            NormalizationInputIdentity.model_validate(request["inputIdentity"])
        )
        != baseline
    )


def test_role_and_nested_map_insertion_order_do_not_change_hash() -> None:
    original = copy.deepcopy(VECTOR["inputIdentity"])
    reordered = copy.deepcopy(original)
    reordered["artifactsByRole"] = dict(reversed(list(reordered["artifactsByRole"].items())))
    normalizer = reordered["targetVersionSet"]["normalizer"]
    normalizer["sourceDialectVersionsByArtifactRole"] = dict(
        reversed(list(normalizer["sourceDialectVersionsByArtifactRole"].items()))
    )
    reordered = dict(reversed(list(reordered.items())))
    left = NormalizationInputIdentity.model_validate(original)
    right = NormalizationInputIdentity.model_validate(reordered)
    assert canonicalize_input_identity(left) == canonicalize_input_identity(right)


def test_unicode_is_utf8_and_unsupported_spec_does_not_hash() -> None:
    changed = copy.deepcopy(VECTOR["inputIdentity"])
    changed["tradingBookId"] = "책/😀"
    identity = NormalizationInputIdentity.model_validate(changed)
    canonical = canonicalize_input_identity(identity)
    assert "책/😀".encode() in canonical
    assert b"\\u" not in canonical
    assert calculate_input_set_hash(identity) != VECTOR["expectedSha256"]
    changed["hashSpecificationVersion"] = "TRADING_INPUT_SET_HASH_V2"
    with pytest.raises(ValueError, match="Unsupported"):
        calculate_input_set_hash(NormalizationInputIdentity.model_validate(changed))


def test_v1_rejects_multiple_source_sessions() -> None:
    changed = copy.deepcopy(VECTOR["inputIdentity"])
    changed["sourceImportSessionIds"].append("session-other")
    with pytest.raises(ValidationError):
        NormalizationInputIdentity.model_validate(changed)
