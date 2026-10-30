"""Verify immutable Analytics inputs and adapt their public fields to the pure Metric Engine."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from refinvest.contracts.analytics.identity import AnalyticsContractError, jcs_sha256
from refinvest.trading_review.analytics.model import (
    AnalysisConfig,
    AnalysisPeriod,
    AnalyticsAllocationInput,
    AnalyticsCanonicalInput,
    AnalyticsCapabilityInput,
    AnalyticsEpisodeInput,
    AnalyticsEvidenceRow,
    AnalyticsExecutionInput,
    FindingMinimums,
    MetricMinimums,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.model import (
    AllocationAction,
    Capability,
    Direction,
    InstrumentDescriptor,
    MoneyAmount,
    ReconciliationStatus,
    ReconstructionStatus,
)

_DECIMAL = re.compile(r"^(0|-?[1-9][0-9]*|-?(0|[1-9][0-9]*)\.[0-9]*[1-9])$")


class AnalyticsInputFailure(AnalyticsContractError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.retryable = retryable


def input_mapping(node: object) -> dict[str, object]:
    if not isinstance(node, dict):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    return cast(dict[str, object], node)


def _array(node: object) -> list[object]:
    if not isinstance(node, list):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    return cast(list[object], node)


def input_integer(node: object) -> int:
    if isinstance(node, bool) or not isinstance(node, int):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    return node


def input_time(node: object) -> datetime:
    if not isinstance(node, str):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    try:
        value = datetime.fromisoformat(node.replace("Z", "+00:00"))
    except ValueError as error:
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED") from error
    if value.tzinfo is None:
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    return value.astimezone(UTC)


def _scaled(node: object) -> ScaledDecimal:
    value = input_mapping(node)
    lexeme = value.get("value", value.get("amount"))
    scale = value.get("sourceScale")
    if (
        not isinstance(lexeme, str)
        or _DECIMAL.fullmatch(lexeme) is None
        or not isinstance(scale, int)
        or scale < 0
    ):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    try:
        return ScaledDecimal.from_decimal(Decimal(lexeme), scale=scale)
    except (ValueError, ArithmeticError) as error:
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED") from error


def _money(node: object) -> MoneyAmount:
    value = input_mapping(node)
    if not isinstance(value.get("currency"), str):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    return MoneyAmount(_scaled(value), str(value["currency"]))


def verify_inputs(
    request: dict[str, object], ledger: dict[str, object], manifest: dict[str, object]
) -> None:
    identity = input_mapping(request["inputIdentity"])
    expected_ledger_fields = {
        "schemaVersion",
        "ledgerRevisionId",
        "ledgerContentHash",
        "canonicalSchemaVersion",
        "reviewUnitSchemaVersion",
        "instrumentDescriptors",
        "canonicalRecords",
        "sourceEvidenceReferences",
        "reviewUnitEngineInputs",
    }
    if (
        set(ledger) != expected_ledger_fields
        or ledger["schemaVersion"] != "TRADING_ANALYTICS_LEDGER_INPUT_V1"
    ):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    if (
        ledger["ledgerRevisionId"] != request["ledgerRevisionId"]
        or ledger["ledgerContentHash"] != identity["ledgerContentHash"]
    ):
        raise AnalyticsInputFailure("INPUT_HASH_MISMATCH")
    if (
        ledger["canonicalSchemaVersion"] != "CANONICAL_V1"
        or ledger["reviewUnitSchemaVersion"] != "REVIEW_UNIT_V1"
    ):
        raise AnalyticsInputFailure("VERSION_INCOMPATIBLE")
    records = _array(ledger["canonicalRecords"])
    record_ids = [str(input_mapping(record).get("recordId")) for record in records]
    if record_ids != sorted(set(record_ids), key=lambda value: value.encode("utf-16-be")):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    # Normalization V1 hashes the public canonical-record section without recordHash.
    computed_ledger_hash = jcs_sha256(
        [
            {key: value for key, value in input_mapping(record).items() if key != "recordHash"}
            for record in records
        ]
    )
    if computed_ledger_hash != identity["ledgerContentHash"]:
        raise AnalyticsInputFailure("INPUT_HASH_MISMATCH")
    expected_manifest_fields = {
        "manifestVersion",
        "manifestHashSpecificationVersion",
        "inputSetHash",
        "executionInputHash",
        "canonicalRecordSetHash",
        "qualityReportHash",
        "instrumentTermsHash",
        "versionSetHash",
        "sourceArtifactHashesByRole",
        "counts",
        "manifestHash",
        "reconstructionPolicyVersion",
        "reconciliationVersion",
        "artifactCoverages",
        "reviewUnits",
        "reconciliationMatches",
        "reconciliationIssues",
        "capability",
        "quantityConservationPassed",
        "cashFlowConservationPassed",
    }
    if set(manifest) != expected_manifest_fields or manifest["manifestVersion"] != "MANIFEST_V1":
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    if manifest.get("manifestHashSpecificationVersion") != "TRADING_NORMALIZATION_RESULT_HASH_V1":
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    actual_manifest_hash = jcs_sha256(
        {key: value for key, value in manifest.items() if key != "manifestHash"}
    )
    if (
        actual_manifest_hash != manifest.get("manifestHash")
        or actual_manifest_hash != input_mapping(identity["reconciliationManifest"])["manifestHash"]
    ):
        raise AnalyticsInputFailure("INPUT_HASH_MISMATCH")
    if manifest.get("canonicalRecordSetHash") != computed_ledger_hash:
        raise AnalyticsInputFailure("INPUT_HASH_MISMATCH")
    capability = _array(manifest.get("capability"))
    if capability != request["capabilitySnapshot"] or capability != sorted(
        set(capability), key=str
    ):
        raise AnalyticsInputFailure("INPUT_HASH_MISMATCH")
    counts = input_mapping(manifest.get("counts"))
    units = _array(manifest.get("reviewUnits"))
    references = _array(ledger["sourceEvidenceReferences"])
    if (
        counts.get("canonicalRecordCount") != len(records)
        or counts.get("reviewUnitCount") != len(units)
        or counts.get("sourceEvidenceSnapshotCount") != len(references)
        or counts.get("positionAllocationCount")
        != sum(len(_array(input_mapping(unit).get("allocations"))) for unit in units)
    ):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")


def _config(request: dict[str, object]) -> AnalysisConfig:
    node = input_mapping(request["analysisConfig"])
    period = input_mapping(node["analysisPeriod"])
    minimums = input_mapping(node["metricMinimums"])
    findings = input_mapping(node["findingMinimums"])
    try:
        ZoneInfo(str(node["reviewTimezone"]))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise AnalyticsInputFailure("ANALYSIS_CONFIG_HASH_MISMATCH") from error
    return AnalysisConfig(
        analysis_period=AnalysisPeriod(
            input_time(period["startInclusive"]), input_time(period["endExclusive"])
        ),
        review_timezone=str(node["reviewTimezone"]),
        timezone_database_version=str(node["timezoneDatabaseVersion"]),
        metric_minimums=MetricMinimums(
            input_integer(minimums["reentryEligibleLosses"]),
            input_integer(minimums["exposurePairs"]),
            input_integer(minimums["priorBaselineEpisodes"]),
            input_integer(minimums["holdingWins"]),
            input_integer(minimums["holdingLosses"]),
        ),
        finding_minimums=FindingMinimums(
            input_integer(findings["reentryEligibleLosses"]),
            input_integer(findings["exposurePairs"]),
            input_integer(findings["holdingWins"]),
            input_integer(findings["holdingLosses"]),
        ),
    )


def adapt_inputs(
    request: dict[str, object], ledger: dict[str, object], manifest: dict[str, object]
) -> tuple[
    tuple[AnalyticsEpisodeInput, ...],
    tuple[AnalyticsCanonicalInput, ...],
    tuple[InstrumentDescriptor, ...],
    AnalysisConfig,
]:
    verify_inputs(request, ledger, manifest)
    raw_descriptors = [input_mapping(item) for item in _array(ledger["instrumentDescriptors"])]
    descriptors = tuple(
        InstrumentDescriptor(
            instrument_id=str(item["instrumentId"]),
            venue_symbol=str(item["venueSymbol"]),
            settlement_asset=str(item["settlementAsset"]),
            quantity_step=ScaledDecimal.parse(str(item["quantityStep"])),
            price_tick=ScaledDecimal.parse(str(item["priceTick"])),
            contract_multiplier=ScaledDecimal.parse(str(item["contractMultiplier"])),
            settlement_precision=input_integer(item["settlementPrecision"]),
            instrument_terms_version=str(item["instrumentTermsVersion"]),
        )
        for item in raw_descriptors
    )
    if len({item.instrument_id for item in descriptors}) != len(descriptors):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    refs = [input_mapping(item) for item in _array(ledger["sourceEvidenceReferences"])]
    if [(str(item["recordId"]), str(item["snapshotId"])) for item in refs] != sorted(
        (str(item["recordId"]), str(item["snapshotId"])) for item in refs
    ):
        raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED")
    by_record: dict[str, list[AnalyticsEvidenceRow]] = {}
    ref_keys: set[tuple[str, str]] = set()
    for ref in refs:
        key = (str(ref["recordId"]), str(ref["snapshotId"]))
        if key in ref_keys or input_integer(ref["sourceRowNumber"]) < 1:
            raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
        ref_keys.add(key)
        by_record.setdefault(key[0], []).append(
            AnalyticsEvidenceRow(
                str(ref["coreArtifactId"]),
                input_integer(ref["sourceRowNumber"]),
                str(ref["sourceRowHash"]),
            )
        )
    records: list[AnalyticsCanonicalInput] = []
    for raw in _array(ledger["canonicalRecords"]):
        node = input_mapping(raw)
        record_id = str(node["recordId"])
        if (record_id, str(node["sourceEvidenceSnapshotId"])) not in ref_keys:
            raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
        provenance = tuple(by_record.get(record_id, []))
        if node["recordType"] == "EXECUTION":
            records.append(AnalyticsExecutionInput(record_id, provenance, _scaled(node["price"])))
        else:
            records.append(AnalyticsCanonicalInput(record_id, provenance))
    if set(by_record) != {item.record_id for item in records}:
        raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
    units = [input_mapping(item) for item in _array(manifest["reviewUnits"])]
    supplements = [input_mapping(item) for item in _array(ledger["reviewUnitEngineInputs"])]
    if [str(item["reviewUnitId"]) for item in supplements] != sorted(
        {str(item["reviewUnitId"]) for item in supplements}
    ):
        raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
    supplemental = {str(item["reviewUnitId"]): item for item in supplements}
    if set(supplemental) != {str(item["reviewUnitId"]) for item in units}:
        raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
    known_records = {item.record_id for item in records}
    known_instruments = {item.instrument_id for item in descriptors}
    episodes: list[AnalyticsEpisodeInput] = []
    for unit in units:
        unit_id = str(unit["reviewUnitId"])
        supplement = supplemental[unit_id]
        allocations = [input_mapping(item) for item in _array(unit["allocations"])]
        extra_allocations = {
            input_mapping(item)["allocationId"]: input_mapping(item)
            for item in _array(supplement["allocationEvidence"])
        }
        if len(extra_allocations) != len(allocations) or set(extra_allocations) != {
            item["allocationId"] for item in allocations
        }:
            raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
        evidence_ids = tuple(str(item) for item in _array(supplement["evidenceRecordIds"]))
        if set(evidence_ids) - known_records or list(evidence_ids) != sorted(set(evidence_ids)):
            raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
        adapted_allocations: list[AnalyticsAllocationInput] = []
        for allocation in allocations:
            extra = extra_allocations[allocation["allocationId"]]
            if allocation["reviewUnitId"] != unit_id or allocation["recordId"] not in known_records:
                raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
            record_ids = tuple(str(item) for item in _array(extra["evidenceRecordIds"]))
            if set(record_ids) - known_records or list(record_ids) != sorted(set(record_ids)):
                raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
            for name in ("allocatedFee", "allocatedReportedPnl"):
                if (
                    name in allocation
                    and allocation[name] is not None
                    and allocation[name] != extra[name]
                ):
                    raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
            adapted_allocations.append(
                AnalyticsAllocationInput(
                    str(allocation["recordId"]),
                    AllocationAction(str(allocation["action"])),
                    _money(extra["allocatedFee"]),
                    _money(extra["allocatedReportedPnl"]),
                    record_ids,
                )
            )
        unit_caps = tuple(Capability(str(item)) for item in _array(unit["capability"]))
        stable = supplement["stableEpisodeTimeAvailable"]
        if not isinstance(stable, bool) or stable != (Capability.STABLE_EPISODE_TIME in unit_caps):
            raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
        if unit["instrumentId"] not in known_instruments:
            raise AnalyticsInputFailure("EVIDENCE_REFERENCE_INVALID")
        episodes.append(
            AnalyticsEpisodeInput(
                episode_id=unit_id,
                instrument_id=str(unit["instrumentId"]),
                direction=Direction(str(unit["direction"])),
                opened_at=input_time(unit["openedAt"])
                if unit.get("openedAt") is not None
                else None,
                closed_at=input_time(unit["closedAt"])
                if unit.get("closedAt") is not None
                else None,
                initial_quantity=_scaled(unit["initialQuantity"])
                if unit.get("initialQuantity") is not None
                else None,
                net_trading_pnl=_money(unit["netTradingPnl"])
                if unit.get("netTradingPnl") is not None
                else None,
                reconstruction_status=ReconstructionStatus(str(unit["reconstructionStatus"])),
                reconciliation_status=ReconciliationStatus(str(unit["reconciliationStatus"])),
                allocations=tuple(adapted_allocations),
                capability=AnalyticsCapabilityInput(unit_caps, stable),
                evidence_record_ids=evidence_ids,
            )
        )
    return tuple(episodes), tuple(records), descriptors, _config(request)
