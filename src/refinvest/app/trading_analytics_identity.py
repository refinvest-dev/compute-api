"""Analytics V1 RFC 8785 identity and strict JSON primitives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import cast

MAX_SAFE_INTEGER = 9_007_199_254_740_991
INPUT_HASH_SPEC = "TRADING_ANALYTICS_INPUT_HASH_V1"
RESULT_HASH_SPEC = "TRADING_ANALYTICS_RESULT_HASH_V1"
RESULT_SCHEMA = "TRADING_ANALYTICS_RESULT_SCHEMA_V1"


class AnalyticsContractError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
        result[key] = value
    return result


def strict_json(payload: bytes) -> dict[str, object]:
    """Parse artifact bytes without accepting duplicate keys or unsafe numbers."""
    if payload.startswith(b"\xef\xbb\xbf"):
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")

    def integer(raw: str) -> int:
        value = int(raw)
        if abs(value) > MAX_SAFE_INTEGER:
            raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
        return value

    def invalid_number(_: str) -> object:
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")

    try:
        node: object = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_int=integer,
            parse_float=invalid_number,
            parse_constant=invalid_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED") from error
    if not isinstance(node, dict):
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
    return cast(dict[str, object], node)


def _jcs(node: object) -> str:
    if node is None:
        return "null"
    if node is True:
        return "true"
    if node is False:
        return "false"
    if isinstance(node, str):
        node.encode("utf-8")
        return json.dumps(node, ensure_ascii=False, separators=(",", ":"))
    if isinstance(node, int):
        if abs(node) > MAX_SAFE_INTEGER:
            raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
        return str(node)
    if isinstance(node, Mapping):
        mapping = cast(Mapping[object, object], node)
        if any(not isinstance(key, str) for key in mapping):
            raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
        string_mapping = cast(Mapping[str, object], node)
        return (
            "{"
            + ",".join(
                _jcs(key) + ":" + _jcs(string_mapping[key])
                for key in sorted(string_mapping, key=lambda item: item.encode("utf-16-be"))
            )
            + "}"
        )
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], node)
        return "[" + ",".join(_jcs(item) for item in sequence) + "]"
    raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")


def canonical_bytes(node: object) -> bytes:
    return _jcs(node).encode("utf-8")


def jcs_sha256(node: object) -> str:
    return hashlib.sha256(canonical_bytes(node)).hexdigest()


def _object(node: object, code: str) -> dict[str, object]:
    if not isinstance(node, dict):
        raise AnalyticsContractError(code)
    return cast(dict[str, object], node)


def _artifact_identity(reference: object, *, manifest: bool = False) -> dict[str, object]:
    source = _object(reference, "INPUT_SCHEMA_UNSUPPORTED")
    required = ("coreReferenceId", "mediaType", "byteSize", "sha256")
    if not all(key in source for key in required):
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
    result = {key: source[key] for key in required}
    if manifest:
        if "manifestHash" not in source:
            raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
        result["manifestHash"] = source["manifestHash"]
    return result


def analysis_config_hash(config: object) -> str:
    return jcs_sha256(config)


def analysis_input_hash(identity: object) -> str:
    source = _object(identity, "INPUT_SCHEMA_UNSUPPORTED")
    if source.get("hashSpecificationVersion") != INPUT_HASH_SPEC:
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
    return jcs_sha256(source)


def create_fingerprint(request: Mapping[str, object]) -> str:
    """All immutable create fields; locator and expiry never enter this digest."""
    names = (
        "coreLogicalJobId",
        "coreAnalysisRunId",
        "ledgerRevisionId",
        "inputIdentity",
        "expectedInputHash",
        "inputHashSpecificationVersion",
        "analysisConfig",
        "analysisConfigHash",
        "targetVersionSet",
        "capabilitySnapshot",
        "resultSchemaVersion",
        "resultHashSpecificationVersion",
        "attemptToken",
        "deletionGeneration",
        "retryPolicyVersion",
        "resultDeliveryPolicy",
    )
    if any(name not in request for name in names):
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
    projection = {name: request[name] for name in names}
    projection["ledgerRevisionContent"] = _artifact_identity(request.get("ledgerRevisionContent"))
    projection["reconciliationManifest"] = _artifact_identity(request.get("reconciliationManifest"))
    return jcs_sha256(projection)


def validate_create_identity(request: Mapping[str, object]) -> None:
    identity = _object(request.get("inputIdentity"), "INPUT_SCHEMA_UNSUPPORTED")
    if request.get("inputHashSpecificationVersion") != INPUT_HASH_SPEC:
        raise AnalyticsContractError("INPUT_SCHEMA_UNSUPPORTED")
    if (
        request.get("resultSchemaVersion") != RESULT_SCHEMA
        or request.get("resultHashSpecificationVersion") != RESULT_HASH_SPEC
    ):
        raise AnalyticsContractError("VERSION_INCOMPATIBLE")
    if request.get("ledgerRevisionId") != identity.get("ledgerRevisionId"):
        raise AnalyticsContractError("INPUT_HASH_MISMATCH")
    if request.get("targetVersionSet") != identity.get("targetVersionSet"):
        raise AnalyticsContractError("INPUT_HASH_MISMATCH")
    if request.get("capabilitySnapshot") != identity.get("capabilitySnapshot"):
        raise AnalyticsContractError("INPUT_HASH_MISMATCH")
    if _artifact_identity(request.get("ledgerRevisionContent")) != identity.get(
        "ledgerRevisionContent"
    ):
        raise AnalyticsContractError("ARTIFACT_IDENTITY_CONFLICT")
    manifest = _object(identity.get("reconciliationManifest"), "INPUT_SCHEMA_UNSUPPORTED")
    for key, value in _artifact_identity(request.get("reconciliationManifest")).items():
        if manifest.get(key) != value:
            raise AnalyticsContractError("ARTIFACT_IDENTITY_CONFLICT")
    if analysis_config_hash(request.get("analysisConfig")) != request.get("analysisConfigHash"):
        raise AnalyticsContractError("ANALYSIS_CONFIG_HASH_MISMATCH")
    if request.get("analysisConfigHash") != identity.get("analysisConfigHash"):
        raise AnalyticsContractError("ANALYSIS_CONFIG_HASH_MISMATCH")
    if analysis_input_hash(identity) != request.get("expectedInputHash"):
        raise AnalyticsContractError("INPUT_HASH_MISMATCH")


def require_supported_version_set(value: object, implementation_digest: str) -> None:
    """Admit only the deployed immutable build and the three implemented V1 definitions."""
    version_set = _object(value, "VERSION_INCOMPATIBLE")
    if (
        version_set.get("metricDefinitionVersionsByMetricId")
        != {
            "ENTRY_WITHIN_WINDOW_AFTER_LOSS": "1",
            "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS": "1",
            "WIN_LOSS_HOLDING_DURATION_DIFFERENCE": "1",
        }
        or version_set.get("findingRuleSetVersion") != "TRADING_FINDING_RULES_DISABLED_V1"
        or version_set.get("analysisConfigVersion") != "ANALYSIS_CONFIG_V1"
        or version_set.get("evidenceSchemaVersion") != "TRADING_EVIDENCE_V1"
        or version_set.get("implementationDigest") != implementation_digest
    ):
        raise AnalyticsContractError("VERSION_INCOMPATIBLE")
