"""OpenAPI serialization for production normalization results."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from decimal import localcontext
from typing import cast

from refinvest.app.trading_review_result_integrity import (
    RESULT_HASH_SPECIFICATION_V1,
    RESULT_SCHEMA_V1,
    RESULT_SCHEMA_V2,
    ResultIntegrityError,
    finalize_public_result,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.hashing import stable_id
from refinvest.trading_review.model import (
    CanonicalRecord,
    CapabilitySnapshot,
    ExecutionRecord,
    FeeRecord,
    FuturesPositionEpisode,
    InstrumentDescriptor,
    MoneyAmount,
    NormalizationResult,
    SourceArtifact,
    SourceArtifactRole,
    SourceRowProvenance,
    VenueReportedPnlRecord,
)
from refinvest.trading_review.parser import parse_artifact

MEDIA_TYPE = "application/vnd.refinvest.trading-normalization+json"

_TRADE_EVIDENCE_FIELDS = (
    ("Time", "Time"),
    ("Symbol", "Symbol"),
    ("Side", "Side"),
    ("Price", "Price"),
    ("Quantity", "Quantity"),
    ("Amount", "Amount"),
    ("Fee", "Fee"),
    ("Realized Profit", "Realized Profit"),
    ("Buyer", "Buyer"),
    ("Maker", "Maker"),
)
_POSITION_EVIDENCE_FIELDS = (
    ("Symbol", "Symbol"),
    ("Margin Mode", "Margin Mode"),
    ("Position Side", "Position Side"),
    ("Entry Price", "Entry Price"),
    ("Avg. Close Price", "Avg. Close Price"),
    ("Max Open Interest", "Max Open Interest"),
    ("Closed Vol.", "Closed Vol."),
    ("Closing PNL", "Closing PNL"),
    ("Opened", "Opened"),
    ("Closed", "Closed"),
    ("Status", "Status"),
)


def build_normalization_payload(
    result: NormalizationResult,
    *,
    job_id: str,
    core_logical_job_id: str,
    core_import_session_id: str,
    core_normalization_run_id: str,
    attempt_token: str,
    deletion_generation: int,
    input_set_hash: str,
    hash_specification_version: str,
    target_version_set: dict[str, object],
    artifacts: tuple[SourceArtifact, ...],
    masking_key: bytes,
    created_at: datetime,
    terminal_at: datetime,
    instrument_terms: tuple[InstrumentDescriptor, ...],
    result_schema_version: str = RESULT_SCHEMA_V1,
    result_hash_specification_version: str = RESULT_HASH_SPECIFICATION_V1,
) -> dict[str, object]:
    """Build complete, evidence-linked terminal payload without source UID/IDs."""
    snapshots = _evidence_snapshots(artifacts, masking_key=masking_key, captured_at=terminal_at)
    snapshot_by_source: dict[tuple[str, int], str] = {}
    for item in snapshots:
        snapshot_by_source[(str(item["coreArtifactId"]), int(str(item["sourceRowNumber"])))] = str(
            item["snapshotId"]
        )

    settlement_by_instrument = {
        item.instrument_id: item.settlement_asset for item in instrument_terms
    }
    records: list[dict[str, object]] = []
    for record in result.canonical_records:
        provenance = _record_provenance(record)
        if not provenance:
            raise ValueError("Canonical record has no source evidence.")
        primary = provenance[0]
        snapshot_id = snapshot_by_source[(primary.artifact_id, primary.source_row_number)]
        records.append(_canonical_record(record, snapshot_id, settlement_by_instrument))

    coverages = [
        {
            "artifactRole": coverage.role.value,
            "earliestObservedAt": _timestamp(coverage.earliest_observed_at),
            "latestObservedAt": _timestamp(coverage.latest_observed_at),
            "rowCount": coverage.row_count,
            "sourceTimezone": coverage.source_timezone,
            "timestampPrecision": coverage.timestamp_precision,
            "coverageCompleteness": coverage.completeness_assertion,
        }
        for coverage in result.artifact_coverages
    ]
    coverages.sort(key=lambda item: str(item["artifactRole"]))
    executions = {
        record.record_id: record
        for record in result.canonical_records
        if isinstance(record, ExecutionRecord)
    }
    terms_by_instrument = {item.instrument_id: item for item in instrument_terms}
    review_units = [
        _review_unit(episode, executions, terms_by_instrument) for episode in result.episodes
    ]
    supplement = None
    if result_schema_version == RESULT_SCHEMA_V2:
        supplement = _analytics_input_supplement(result.episodes, review_units)
    manifest = {
        "manifestVersion": result.reconciliation_manifest.manifest_schema_version,
        "inputSetHash": input_set_hash,
        "executionInputHash": result.input_hash,
        "instrumentTermsHash": result.instrument_terms_hash,
        "versionSetHash": target_version_set["versionSetHash"],
        "sourceArtifactHashesByRole": {item.role.value: item.content_hash for item in artifacts},
        "reconstructionPolicyVersion": result.reconciliation_manifest.reconstruction_policy_version,
        "reconciliationVersion": result.reconciliation_manifest.reconciliation_version,
        "artifactCoverages": coverages,
        "reviewUnits": review_units,
        "reconciliationMatches": [
            {
                "matchId": stable_id(
                    "reconciliation_match", {"summaryRecordId": item.summary_record_id}
                ),
                "summaryRecordId": item.summary_record_id,
                "reviewUnitId": item.episode_id,
                "status": item.status.value,
                "candidateReviewUnitIds": list(item.candidate_episode_ids),
                "deltas": [
                    {
                        "field": delta.field,
                        "actual": delta.actual.source_lexeme,
                        "expected": delta.expected.source_lexeme,
                        "absoluteDelta": delta.absolute_delta.source_lexeme,
                        "tolerance": delta.tolerance.source_lexeme,
                    }
                    for delta in item.deltas
                ],
                "evidenceRecordIds": [
                    record_id
                    for record_id in item.evidence_record_ids
                    if record_id != item.episode_id
                ],
            }
            for item in result.reconciliation_manifest.matches
        ],
        "reconciliationIssues": [
            {
                "issueId": stable_id(
                    "reconciliation_issue",
                    {
                        "code": item.code,
                        "reviewUnitId": item.episode_id,
                        "summaryRecordId": item.summary_record_id,
                        "evidenceRecordIds": sorted(item.evidence_record_ids),
                    },
                ),
                "code": item.code,
                "severity": item.severity,
                "reviewUnitId": item.episode_id,
                "summaryRecordId": item.summary_record_id,
                "evidenceRecordIds": [
                    record_id
                    for record_id in item.evidence_record_ids
                    if record_id != item.episode_id
                ],
            }
            for item in result.reconciliation_manifest.issues
        ],
        "capability": [item.value for item in result.capability.capabilities],
        "quantityConservationPassed": result.reconciliation_manifest.quantity_conservation_passed,
        "cashFlowConservationPassed": (
            result.reconciliation_manifest.fee_conservation_passed
            and result.reconciliation_manifest.pnl_conservation_passed
        ),
    }
    quality = {
        "qualityStatus": result.quality_report.quality_status.value,
        "rowCountsByArtifactRole": {
            role.value: count for role, count in result.quality_report.source_row_counts
        },
        "ignoredNonTradingRowCounts": {role.value: 0 for role in SourceArtifactRole},
        "validationIssues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "artifactRole": issue.artifact_role.value,
                "sourceRowNumber": issue.source_row_number,
                "field": issue.field,
            }
            for issue in result.quality_report.validation_issues
        ],
        "reconciliationStatusCounts": {
            status.value: count
            for status, count in result.quality_report.reconciliation_status_counts
        },
        "excludedReasonCounts": dict(result.quality_report.exclusion_reason_counts),
    }
    payload: dict[str, object] = {
        "jobId": job_id,
        "coreLogicalJobId": core_logical_job_id,
        "coreNormalizationRunId": core_normalization_run_id,
        "coreImportSessionId": core_import_session_id,
        "attemptToken": attempt_token,
        "deletionGeneration": deletion_generation,
        "status": "COMPLETED",
        "targetVersionSet": target_version_set,
        "inputSetHash": input_set_hash,
        "hashSpecificationVersion": hash_specification_version,
        "resultSchemaVersion": result_schema_version,
        "resultHashSpecificationVersion": result_hash_specification_version,
        "artifactCoverages": coverages,
        "canonicalRecords": records,
        "sourceEvidenceSnapshots": snapshots,
        "reconciliationManifest": manifest,
        "capability": [item.value for item in result.capability.capabilities],
        "qualityReport": quality,
        "_artifactIdsByRole": {item.role.value: item.artifact_id for item in artifacts},
        "createdAt": _timestamp(created_at),
        "terminalAt": _timestamp(terminal_at),
    }
    if supplement is not None:
        payload["analyticsInputSupplement"] = supplement
    return finalize_public_result(payload)


def _evidence_snapshots(
    artifacts: tuple[SourceArtifact, ...], *, masking_key: bytes, captured_at: datetime
) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    for artifact in sorted(artifacts, key=lambda item: item.role.value):
        parsed = parse_artifact(artifact)
        fields = (
            _TRADE_EVIDENCE_FIELDS
            if artifact.role is SourceArtifactRole.TRADE_HISTORY
            else _POSITION_EVIDENCE_FIELDS
        )
        for row in parsed.rows:
            values = dict(row.values)
            evidence: dict[str, object] = {
                "snapshotId": stable_id(
                    "source_evidence",
                    {"artifactId": artifact.artifact_id, "row": row.source_row_number},
                ),
                "coreArtifactId": artifact.artifact_id,
                "artifactRole": artifact.role.value,
                "sourceRowNumber": row.source_row_number,
                "sourceRowHash": row.raw_row_hash,
                "dialect": artifact.dialect,
                "maskingPolicyVersion": "SOURCE_EVIDENCE_MASK_V1",
                "allowlistedFields": [
                    {"name": output_name, "value": values[source_name]}
                    for source_name, output_name in fields
                ],
                "capturedAt": _timestamp(captured_at),
            }
            if artifact.role is SourceArtifactRole.TRADE_HISTORY:
                evidence["maskedTradeId"] = _masked_alias(
                    masking_key, artifact.artifact_id, "trade", values["Trade ID"]
                )
                evidence["maskedOrderId"] = _masked_alias(
                    masking_key, artifact.artifact_id, "order", values["Order ID"]
                )
            snapshots.append(evidence)
    return snapshots


def _masked_alias(key: bytes, artifact_id: str, kind: str, value: str) -> str:
    message = f"{artifact_id}\0{kind}\0{value}".encode()
    return f"{kind[:3]}_{hmac.new(key, message, hashlib.sha256).hexdigest()[:24]}"


def _canonical_record(
    record: CanonicalRecord, snapshot_id: str, settlement_by_instrument: dict[str, str]
) -> dict[str, object]:
    base = {
        "recordId": record.record_id,
        "instrumentId": record.instrument_id,
        "sourceEvidenceSnapshotId": snapshot_id,
        "recordHash": record.record_hash,
    }
    if isinstance(record, ExecutionRecord):
        return {
            **base,
            "recordType": "EXECUTION",
            "sourceExecutionKey": record.source_execution_key,
            "occurredAt": _timestamp(record.occurred_at),
            "side": record.side.value,
            "price": _quantity_or_price(record.price),
            "quantity": _quantity_or_price(record.quantity),
            "executionOrigin": record.execution_origin,
        }
    if isinstance(record, FeeRecord):
        return {
            **base,
            "recordType": "FEE",
            "sourceExecutionKey": record.source_execution_key,
            "occurredAt": _timestamp(record.occurred_at),
            "sourceAmount": _money(record.source_amount, record.asset),
            "feeKind": record.fee_kind.value,
        }
    if isinstance(record, VenueReportedPnlRecord):
        return {
            **base,
            "recordType": "VENUE_REPORTED_PNL",
            "sourceExecutionKey": record.source_execution_key,
            "occurredAt": _timestamp(record.occurred_at),
            "realizedPnl": _money(record.realized_pnl, record.settlement_asset),
        }
    return {
        **base,
        "recordType": "VENUE_POSITION_SUMMARY",
        "openedAt": _timestamp(record.opened_at),
        "closedAt": _timestamp(record.closed_at),
        "direction": record.direction.value,
        "marginMode": record.margin_mode.value,
        "entryPrice": _quantity_or_price(record.entry_price),
        "averageClosePrice": _quantity_or_price(record.average_close_price),
        "maxOpenInterest": _quantity_or_price(record.max_open_interest),
        "closedVolume": _quantity_or_price(record.closed_volume),
        "closingPnl": _money(record.closing_pnl, settlement_by_instrument[record.instrument_id]),
        "sourceStatus": "CLOSED",
    }


def _review_unit(
    episode: FuturesPositionEpisode,
    executions: dict[str, ExecutionRecord],
    terms: dict[str, InstrumentDescriptor],
) -> dict[str, object]:
    allocations: list[dict[str, object]] = []
    for item in episode.allocations:
        if not isinstance(cast(object, item.allocated_fee), MoneyAmount) or not isinstance(
            cast(object, item.allocated_reported_pnl), MoneyAmount
        ):
            raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
        allocations.append(
            {
                "allocationId": stable_id(
                    "allocation",
                    {
                        "recordId": item.execution_record_id,
                        "reviewUnitId": item.episode_id,
                        "action": item.action.value,
                    },
                ),
                "recordId": item.execution_record_id,
                "reviewUnitId": item.episode_id,
                "action": item.action.value,
                "allocatedQuantity": _quantity_or_price(item.allocated_quantity),
                "allocatedFee": _money(item.allocated_fee.amount, item.allocated_fee.asset),
                "allocatedReportedPnl": _money(
                    item.allocated_reported_pnl.amount, item.allocated_reported_pnl.asset
                ),
            }
        )
    net = episode.net_trading_pnl
    initial = episode.initial_quantity
    notional = None
    if initial is not None:
        first_open = next(
            (
                item
                for item in episode.allocations
                if item.action.value in {"OPEN", "REVERSAL_OPEN"}
            ),
            None,
        )
        if first_open is None:
            raise ValueError("Initial quantity has no opening execution.")
        opening_price = executions[first_open.execution_record_id].price
        instrument = terms[episode.instrument_id]
        multiplier = instrument.contract_multiplier
        precision = (
            sum(
                len(value.value.as_tuple().digits) for value in (initial, opening_price, multiplier)
            )
            + 1
        )
        with localcontext() as context:
            context.prec = max(precision, 28)
            amount = ScaledDecimal.from_decimal(
                initial.value * opening_price.value * multiplier.value,
                scale=initial.scale + opening_price.scale + multiplier.scale,
            )
        notional = _money(amount, instrument.settlement_asset)
    return {
        "reviewUnitId": episode.episode_id,
        "type": "FUTURES_POSITION_EPISODE",
        "instrumentId": episode.instrument_id,
        "direction": episode.direction.value,
        "openedAt": _timestamp(episode.opened_at),
        "closedAt": _timestamp(episode.closed_at),
        "initialQuantity": _quantity_or_price(initial) if initial is not None else None,
        "initialNotional": notional,
        "outcomeClassification": _outcome_classification(net),
        "netTradingPnl": _money(net.amount, net.asset) if net is not None else None,
        "reconstructionStatus": episode.reconstruction_status.value,
        "reconciliationStatus": episode.reconciliation_status.value,
        "allocations": allocations,
        "capability": [item.value for item in episode.capability.capabilities],
        "exclusionReasons": list(episode.exclusion_reasons),
    }


def _analytics_input_supplement(
    episodes: tuple[FuturesPositionEpisode, ...], review_units: list[dict[str, object]]
) -> dict[str, object]:
    public_by_id = {str(unit["reviewUnitId"]): unit for unit in review_units}
    if len(public_by_id) != len(review_units) or len(episodes) != len(review_units):
        raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
    rows: list[dict[str, object]] = []
    for episode in episodes:
        if not isinstance(cast(object, episode.capability), CapabilitySnapshot):
            raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
        public = public_by_id.get(episode.episode_id)
        if public is None:
            raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")
        public_allocations = {
            str(item["allocationId"]): item
            for item in cast(list[dict[str, object]], public["allocations"])
        }
        if len(public_allocations) != len(episode.allocations):
            raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
        allocation_rows: list[dict[str, object]] = []
        for allocation in episode.allocations:
            allocation_id = stable_id(
                "allocation",
                {
                    "recordId": allocation.execution_record_id,
                    "reviewUnitId": episode.episode_id,
                    "action": allocation.action.value,
                },
            )
            if allocation_id not in public_allocations:
                raise ResultIntegrityError("EVIDENCE_REFERENCE_INVALID")
            allocation_rows.append(
                {
                    "allocationId": allocation_id,
                    "evidenceRecordIds": _direct_evidence_ids(allocation.evidence_record_ids),
                    "allocatedFee": _money(
                        allocation.allocated_fee.amount, allocation.allocated_fee.asset
                    ),
                    "allocatedReportedPnl": _money(
                        allocation.allocated_reported_pnl.amount,
                        allocation.allocated_reported_pnl.asset,
                    ),
                }
            )
        allocation_rows.sort(key=lambda item: _utf16_key(str(item["allocationId"])))
        rows.append(
            {
                "reviewUnitId": episode.episode_id,
                "evidenceRecordIds": _direct_evidence_ids(episode.evidence_record_ids),
                "stableEpisodeTimeAvailable": episode.capability.stable_episode_time_available,
                "allocationEvidence": allocation_rows,
            }
        )
    rows.sort(key=lambda item: _utf16_key(str(item["reviewUnitId"])))
    return {
        "schemaVersion": "TRADING_ANALYTICS_INPUT_SUPPLEMENT_V1",
        "reviewUnitEngineInputs": rows,
    }


def _utf16_key(value: str) -> bytes:
    return value.encode("utf-16-be")


def _direct_evidence_ids(value: object) -> list[str]:
    if not isinstance(value, tuple) or not value:
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    items = cast(tuple[object, ...], value)
    if any(not isinstance(item, str) or not item for item in items):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    ids = cast(tuple[str, ...], value)
    if len(ids) != len(set(ids)):
        raise ResultIntegrityError("RESULT_ITEM_CONFLICT")
    return sorted(ids, key=_utf16_key)


def _quantity_or_price(value: ScaledDecimal | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"value": value.source_lexeme, "sourceScale": value.scale}


def _money(value: ScaledDecimal, currency: str) -> dict[str, object]:
    if (
        not isinstance(cast(object, value), ScaledDecimal)
        or not isinstance(cast(object, currency), str)
        or not currency
    ):
        raise ResultIntegrityError("RESULT_SCHEMA_UNSUPPORTED")
    return {"amount": value.source_lexeme, "currency": currency, "sourceScale": value.scale}


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _outcome_classification(value: MoneyAmount | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    amount = value.amount.value
    return "LOSS" if amount < 0 else "WIN" if amount > 0 else "BREAKEVEN"


def _record_provenance(record: CanonicalRecord) -> tuple[SourceRowProvenance, ...]:
    return record.provenance
