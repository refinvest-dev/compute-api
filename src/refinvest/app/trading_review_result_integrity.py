"""ADR-066 integrity over the public normalization DTO, never engine dataclasses."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import cast

RESULT_SCHEMA_V1 = "TRADING_NORMALIZATION_RESULT_V1"
RESULT_HASH_SPECIFICATION_V1 = "TRADING_NORMALIZATION_RESULT_HASH_V1"
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_SECTIONS = (
    "ARTIFACT_COVERAGES",
    "CANONICAL_RECORDS",
    "CAPABILITIES",
    "QUALITY_REPORT",
    "RECONCILIATION_ISSUES",
    "RECONCILIATION_MANIFEST",
    "RECONCILIATION_MATCHES",
    "REVIEW_UNITS",
    "SOURCE_EVIDENCE_SNAPSHOTS",
)
_DECIMAL_KEYS = frozenset({"value", "amount", "actual", "expected", "absoluteDelta", "tolerance"})
_TIMESTAMP_KEYS = frozenset(
    {"earliestObservedAt", "latestObservedAt", "occurredAt", "openedAt", "closedAt"}
)


class ResultIntegrityError(ValueError):
    """Safe failure code for a result that cannot be published."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utf16_key(value: str) -> bytes:
    return value.encode("utf-16-be")


def _canonical_chunks(node: object) -> Iterator[bytes]:
    if isinstance(node, str):
        node.encode("utf-8")
        yield json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif node is None:
        yield b"null"
    elif isinstance(node, bool):
        yield b"true" if node else b"false"
    elif isinstance(node, int):
        if node < 0 or node > _MAX_SAFE_INTEGER:
            raise ResultIntegrityError("RESULT_COUNT_MISMATCH")
        yield str(node).encode("ascii")
    elif isinstance(node, dict):
        mapping = cast(dict[object, object], node)
        if any(not isinstance(key, str) for key in mapping):
            raise TypeError("Result object keys must be strings")
        string_map = cast(dict[str, object], node)
        yield b"{"
        for index, key in enumerate(sorted(string_map, key=_utf16_key)):
            if index:
                yield b","
            yield from _canonical_chunks(key)
            yield b":"
            yield from _canonical_chunks(string_map[key])
        yield b"}"
    elif isinstance(node, (tuple, list)):
        sequence = cast(list[object] | tuple[object, ...], node)
        yield b"["
        for index, item in enumerate(sequence):
            if index:
                yield b","
            yield from _canonical_chunks(item)
        yield b"]"
    else:
        raise TypeError(f"Unsupported result canonical type: {type(node).__name__}")


def canonicalize_result(value: object) -> bytes:
    """RFC 8785 V1 bytes, streamed into one final transport buffer."""
    output = BytesIO()
    for chunk in _canonical_chunks(value):
        output.write(chunk)
    return output.getvalue()


def parse_result_json_bytes(payload: bytes) -> dict[str, object]:
    """Reject ambiguous JSON before any semantic or transport reload check."""
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
            result[key] = value
        return result

    def reject_number(_: str) -> object:
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")

    def parse_integer(raw: str) -> int:
        number = int(raw)
        if number < 0 or number > _MAX_SAFE_INTEGER:
            raise ResultIntegrityError("RESULT_COUNT_MISMATCH")
        return number

    try:
        document: object = json.loads(
            payload,
            object_pairs_hook=object_from_pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
            parse_int=parse_integer,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED") from error
    if not isinstance(document, dict):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    return cast(dict[str, object], document)


def _hash(value: object) -> str:
    digest = hashlib.sha256()
    for chunk in _canonical_chunks(value):
        digest.update(chunk)
    return digest.hexdigest()


def _decimal(value: str) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED") from error
    if not number.is_finite():
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _timestamp(value: str) -> str:
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("naive timestamp")
    except ValueError as error:
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED") from error
    utc = instant.astimezone(UTC)
    fraction = f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def normalize_public_value(value: object, *, key: str = "") -> object:
    """Normalize contract decimal/timestamp wire values without touching evidence text."""
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        return {name: normalize_public_value(item, key=name) for name, item in mapping.items()}
    if key == "allowlistedFields":
        return value
    if isinstance(value, list):
        return [normalize_public_value(item) for item in cast(list[object], value)]
    if isinstance(value, str) and key in _DECIMAL_KEYS:
        return _decimal(value)
    if isinstance(value, str) and key in _TIMESTAMP_KEYS:
        return _timestamp(value)
    return value


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in cast(list[object], value)
    ):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    return cast(list[dict[str, object]], value)


def _sorted_unique(items: list[dict[str, object]], identity: str) -> list[dict[str, object]]:
    unique: dict[str, dict[str, object]] = {}
    for item in items:
        key = item.get(identity)
        if not isinstance(key, str) or not key:
            raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
        previous = unique.get(key)
        if previous is not None and canonicalize_result(previous) != canonicalize_result(item):
            raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
        unique[key] = item
    return [unique[key] for key in sorted(unique, key=_utf16_key)]


def _sorted_strings(values: object) -> list[str]:
    if not isinstance(values, list) or any(
        not isinstance(item, str) or not item for item in cast(list[object], values)
    ):
        raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
    return sorted(set(cast(list[str], values)), key=_utf16_key)


def _quality(quality: dict[str, object]) -> None:
    issues = _items(quality.get("validationIssues"))
    identities: dict[bytes, dict[str, object]] = {}
    for issue in issues:
        key = canonicalize_result(
            [
                issue.get("artifactRole"),
                issue.get("sourceRowNumber"),
                issue.get("code"),
                issue.get("field"),
                issue.get("severity"),
            ]
        )
        if key in identities and canonicalize_result(identities[key]) != canonicalize_result(issue):
            raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
        identities[key] = issue
    quality["validationIssues"] = [identities[key] for key in sorted(identities)]


def _normalize_manifest(manifest: dict[str, object]) -> None:
    manifest["artifactCoverages"] = _sorted_unique(
        _items(manifest.get("artifactCoverages")), "artifactRole"
    )
    manifest["capability"] = _sorted_strings(manifest.get("capability"))
    units = _items(manifest.get("reviewUnits"))
    for unit in units:
        unit["allocations"] = _sorted_unique(_items(unit.get("allocations")), "allocationId")
        unit["capability"] = _sorted_strings(unit.get("capability"))
        unit["exclusionReasons"] = _sorted_strings(unit.get("exclusionReasons"))
    manifest["reviewUnits"] = _sorted_unique(units, "reviewUnitId")
    matches = _items(manifest.get("reconciliationMatches"))
    for match in matches:
        match["candidateReviewUnitIds"] = _sorted_strings(match.get("candidateReviewUnitIds"))
        match["evidenceRecordIds"] = _sorted_strings(match.get("evidenceRecordIds"))
        match["deltas"] = _sorted_unique(_items(match.get("deltas")), "field")
    manifest["reconciliationMatches"] = _sorted_unique(matches, "matchId")
    issues = _items(manifest.get("reconciliationIssues"))
    for issue in issues:
        issue["evidenceRecordIds"] = _sorted_strings(issue.get("evidenceRecordIds"))
    manifest["reconciliationIssues"] = _sorted_unique(issues, "issueId")


def _validate_references(payload: dict[str, object], manifest: dict[str, object]) -> None:
    records = _items(payload["canonicalRecords"])
    snapshots = _items(payload["sourceEvidenceSnapshots"])
    units = _items(manifest["reviewUnits"])
    record_ids = {str(item["recordId"]) for item in records}
    snapshot_ids = {str(item["snapshotId"]) for item in snapshots}
    unit_ids = {str(item["reviewUnitId"]) for item in units}
    source_hashes = cast(dict[str, object], manifest["sourceArtifactHashesByRole"])
    artifact_ids = cast(dict[str, object], payload.get("_artifactIdsByRole", {}))
    for snapshot in snapshots:
        role = snapshot.get("artifactRole")
        if (
            not isinstance(role, str)
            or role not in source_hashes
            or (artifact_ids and snapshot.get("coreArtifactId") != artifact_ids.get(role))
        ):
            raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")
    for record in records:
        if record.get("sourceEvidenceSnapshotId") not in snapshot_ids:
            raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")
    for unit in units:
        for allocation in _items(unit["allocations"]):
            if (
                allocation.get("recordId") not in record_ids
                or allocation.get("reviewUnitId") != unit["reviewUnitId"]
            ):
                raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")
    for match in _items(manifest["reconciliationMatches"]):
        if (
            match.get("summaryRecordId") not in record_ids
            or (match.get("reviewUnitId") is not None and match.get("reviewUnitId") not in unit_ids)
            or any(
                item not in unit_ids for item in cast(list[str], match["candidateReviewUnitIds"])
            )
            or any(item not in record_ids for item in cast(list[str], match["evidenceRecordIds"]))
        ):
            raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")
    for issue in _items(manifest["reconciliationIssues"]):
        if (
            (issue.get("reviewUnitId") is not None and issue.get("reviewUnitId") not in unit_ids)
            or (
                issue.get("summaryRecordId") is not None
                and issue.get("summaryRecordId") not in record_ids
            )
            or any(item not in record_ids for item in cast(list[str], issue["evidenceRecordIds"]))
        ):
            raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")


def semantic_section_values(public: dict[str, object]) -> dict[str, object]:
    """Expose the V1 public DTO projection consumed by section hashing."""
    manifest = cast(dict[str, object], public["reconciliationManifest"])
    return {
        "ARTIFACT_COVERAGES": public["artifactCoverages"],
        "CANONICAL_RECORDS": [
            {key: value for key, value in record.items() if key != "recordHash"}
            for record in _items(public["canonicalRecords"])
        ],
        "SOURCE_EVIDENCE_SNAPSHOTS": [
            {key: value for key, value in item.items() if key != "capturedAt"}
            for item in _items(public["sourceEvidenceSnapshots"])
        ],
        "REVIEW_UNITS": manifest["reviewUnits"],
        "CAPABILITIES": public["capability"],
        "QUALITY_REPORT": public["qualityReport"],
        "RECONCILIATION_MATCHES": manifest["reconciliationMatches"],
        "RECONCILIATION_ISSUES": manifest["reconciliationIssues"],
        "RECONCILIATION_MANIFEST": {
            key: value for key, value in manifest.items() if key != "manifestHash"
        },
    }


def finalize_public_result(payload: dict[str, object]) -> dict[str, object]:
    """Sort the public DTO, validate its references, and attach all V1 digests."""
    if payload.get("hashSpecificationVersion") != "TRADING_INPUT_SET_HASH_V1":
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    normalized = normalize_public_value(payload)
    if not isinstance(normalized, dict):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    public = cast(dict[str, object], normalized)
    public["artifactCoverages"] = _sorted_unique(
        _items(public.get("artifactCoverages")), "artifactRole"
    )
    public["canonicalRecords"] = _sorted_unique(_items(public.get("canonicalRecords")), "recordId")
    snapshots = _items(public.get("sourceEvidenceSnapshots"))
    for snapshot in snapshots:
        snapshot["allowlistedFields"] = _sorted_unique(
            _items(snapshot.get("allowlistedFields")), "name"
        )
    snapshots = _sorted_unique(snapshots, "snapshotId")
    public["sourceEvidenceSnapshots"] = snapshots
    public["capability"] = _sorted_strings(public.get("capability"))
    quality = cast(dict[str, object], public["qualityReport"])
    _quality(quality)
    manifest = cast(dict[str, object], public["reconciliationManifest"])
    _normalize_manifest(manifest)
    if (
        manifest["artifactCoverages"] != public["artifactCoverages"]
        or manifest["capability"] != public["capability"]
        or manifest["inputSetHash"] != public["inputSetHash"]
        or manifest["versionSetHash"]
        != cast(dict[str, object], public["targetVersionSet"])["versionSetHash"]
    ):
        raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
    _validate_references(public, manifest)
    public.pop("_artifactIdsByRole", None)

    units = _items(manifest["reviewUnits"])
    allocations_by_id: dict[str, dict[str, object]] = {}
    for unit in units:
        for allocation in _items(unit["allocations"]):
            allocation_id = str(allocation["allocationId"])
            previous = allocations_by_id.get(allocation_id)
            if previous is not None and canonicalize_result(previous) != canonicalize_result(
                allocation
            ):
                raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
            allocations_by_id[allocation_id] = allocation
    counts = {
        "artifactCoverageCount": len(_items(public["artifactCoverages"])),
        "canonicalRecordCount": len(_items(public["canonicalRecords"])),
        "sourceEvidenceSnapshotCount": len(_items(public["sourceEvidenceSnapshots"])),
        "reviewUnitCount": len(units),
        "positionAllocationCount": len(allocations_by_id),
        "excludedReviewUnitCount": sum(
            bool(cast(list[object], unit["exclusionReasons"])) for unit in units
        ),
        "capabilityCount": len(public["capability"]),
        "validationIssueCount": len(_items(quality["validationIssues"])),
        "reconciliationMatchCount": len(_items(manifest["reconciliationMatches"])),
        "reconciliationIssueCount": len(_items(manifest["reconciliationIssues"])),
    }
    if counts["artifactCoverageCount"] != 2:
        raise ResultIntegrityError("RESULT_COUNT_MISMATCH")
    public["counts"] = counts
    manifest["counts"] = counts

    def section(section_type: str, value: object, count: int) -> dict[str, object]:
        return {
            "sectionType": section_type,
            "itemCount": count,
            "hashSpecificationVersion": RESULT_HASH_SPECIFICATION_V1,
            "sectionHash": _hash(value),
        }

    section_values = semantic_section_values(public)
    records_semantic = section_values["CANONICAL_RECORDS"]
    evidence_semantic = section_values["SOURCE_EVIDENCE_SNAPSHOTS"]
    digest_by_type = {
        "ARTIFACT_COVERAGES": section(
            "ARTIFACT_COVERAGES", public["artifactCoverages"], counts["artifactCoverageCount"]
        ),
        "CANONICAL_RECORDS": section(
            "CANONICAL_RECORDS", records_semantic, counts["canonicalRecordCount"]
        ),
        "SOURCE_EVIDENCE_SNAPSHOTS": section(
            "SOURCE_EVIDENCE_SNAPSHOTS", evidence_semantic, counts["sourceEvidenceSnapshotCount"]
        ),
        "REVIEW_UNITS": section("REVIEW_UNITS", units, counts["reviewUnitCount"]),
        "CAPABILITIES": section("CAPABILITIES", public["capability"], counts["capabilityCount"]),
        "QUALITY_REPORT": section("QUALITY_REPORT", quality, 1),
        "RECONCILIATION_MATCHES": section(
            "RECONCILIATION_MATCHES",
            manifest["reconciliationMatches"],
            counts["reconciliationMatchCount"],
        ),
        "RECONCILIATION_ISSUES": section(
            "RECONCILIATION_ISSUES",
            manifest["reconciliationIssues"],
            counts["reconciliationIssueCount"],
        ),
    }
    manifest["canonicalRecordSetHash"] = digest_by_type["CANONICAL_RECORDS"]["sectionHash"]
    manifest["qualityReportHash"] = digest_by_type["QUALITY_REPORT"]["sectionHash"]
    manifest["manifestHashSpecificationVersion"] = RESULT_HASH_SPECIFICATION_V1
    manifest_semantic = semantic_section_values(public)["RECONCILIATION_MANIFEST"]
    manifest_hash = _hash(manifest_semantic)
    manifest["manifestHash"] = manifest_hash
    digest_by_type["RECONCILIATION_MANIFEST"] = section(
        "RECONCILIATION_MANIFEST", manifest_semantic, 1
    )
    digests = [digest_by_type[name] for name in _SECTIONS]
    digest_manifest = {
        "resultHashSpecificationVersion": RESULT_HASH_SPECIFICATION_V1,
        "resultSchemaVersion": RESULT_SCHEMA_V1,
        "inputSetHash": public["inputSetHash"],
        "inputHashSpecificationVersion": public["hashSpecificationVersion"],
        "targetVersionSet": public["targetVersionSet"],
        "counts": counts,
        "sectionDigests": digests,
        "reconciliationManifestHash": manifest_hash,
    }
    public["resultSchemaVersion"] = RESULT_SCHEMA_V1
    public["resultHashSpecificationVersion"] = RESULT_HASH_SPECIFICATION_V1
    public["sectionDigests"] = digests
    public["resultDigestManifest"] = digest_manifest
    public["ledgerContentHash"] = digest_by_type["CANONICAL_RECORDS"]["sectionHash"]
    public["resultHash"] = _hash(digest_manifest)
    return public


def verify_public_result(payload: dict[str, object]) -> None:
    """Recompute integrity independently from the received public DTO fields."""
    if (
        payload.get("resultSchemaVersion") != RESULT_SCHEMA_V1
        or payload.get("resultHashSpecificationVersion") != RESULT_HASH_SPECIFICATION_V1
    ):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    declared_counts = payload.get("counts")
    declared_sections = payload.get("sectionDigests")
    raw_manifest = payload.get("reconciliationManifest")
    if not isinstance(raw_manifest, dict):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    manifest = cast(dict[str, object], raw_manifest)
    declared_manifest_counts = manifest.get("counts")
    declared_manifest_hash = manifest.get("manifestHash")
    declared_digest_manifest = payload.get("resultDigestManifest")
    declared_result_hash = payload.get("resultHash")
    rebuilt = finalize_public_result(payload)
    if declared_counts != rebuilt["counts"] or declared_manifest_counts != rebuilt["counts"]:
        raise ResultIntegrityError("RESULT_COUNT_MISMATCH")
    if declared_sections != rebuilt["sectionDigests"]:
        raise ResultIntegrityError("RESULT_SECTION_HASH_MISMATCH")
    if payload.get("ledgerContentHash") != rebuilt["ledgerContentHash"]:
        raise ResultIntegrityError("RESULT_SECTION_HASH_MISMATCH")
    rebuilt_manifest = cast(dict[str, object], rebuilt["reconciliationManifest"])
    if declared_manifest_hash != rebuilt_manifest["manifestHash"]:
        raise ResultIntegrityError("RECONCILIATION_MANIFEST_HASH_MISMATCH")
    if (
        declared_digest_manifest != rebuilt["resultDigestManifest"]
        or declared_result_hash != rebuilt["resultHash"]
    ):
        raise ResultIntegrityError("RESULT_HASH_MISMATCH")
