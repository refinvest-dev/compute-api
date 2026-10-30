"""Deterministic Analytics-only workloads, derived from the public synthetic vector.

These exercise the Analytics boundary, not the upstream Normalization runtime.
Opaque upstream pins and the synthetic VersionSet come from the unchanged vector.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from refinvest.contracts.analytics.identity import (
    analysis_config_hash,
    analysis_input_hash,
    canonical_bytes,
    jcs_sha256,
    validate_create_identity,
)

ROOT = Path(__file__).resolve().parents[1]
VECTOR = ROOT / "tests/fixtures/trading_review/analytics_protocol/v1/input-identity-v1.json"
ROLES = ("ledgerRevisionContent", "reconciliationManifest")


def synthetic_inputs(
    episodes: int, *, spacing_seconds: int = 2, evidence_per_record: int = 1
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Build alternating loss/win, nonoverlapping long episodes on one instrument."""
    if not 4 <= episodes <= 1024 or not 1 <= evidence_per_record <= 32:
        raise ValueError("benchmark safety range: 4..1024 episodes, 1..32 evidence per record")
    if not 2 <= spacing_seconds <= 3600:
        raise ValueError("spacing_seconds must be 2..3600")
    vector = json.loads(VECTOR.read_text(encoding="utf-8"))
    ledger, manifest = (copy.deepcopy(vector["artifactSamples"][role]) for role in ROLES)
    record_template = ledger["canonicalRecords"][0]
    unit_template = manifest["reviewUnits"][0]
    ledger["canonicalRecords"] = []
    ledger["sourceEvidenceReferences"] = []
    ledger["reviewUnitEngineInputs"] = []
    manifest["reviewUnits"] = []
    capabilities = sorted(["CLOSED_OUTCOME", "FEES", "INITIAL_EXPOSURE", "STABLE_EPISODE_TIME"])
    manifest["capability"] = capabilities
    start = datetime(2025, 1, 1, tzinfo=UTC)

    def timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def money(amount: str) -> dict[str, Any]:
        return {"amount": amount, "currency": "USDT", "sourceScale": 0}

    for index in range(episodes):
        unit_id = f"unit-{index:06d}"
        opened = start + timedelta(seconds=index * spacing_seconds)
        closed = opened + timedelta(seconds=1)
        pnl = "-1" if index % 2 == 0 else "1"
        record_ids: list[str] = []
        allocations: list[dict[str, Any]] = []
        extras: list[dict[str, Any]] = []
        for leg, action in enumerate(("OPEN", "CLOSE")):
            record_id = f"record-{index:06d}-{leg}"
            allocation_id = f"allocation-{index:06d}-{leg}"
            record_ids.append(record_id)
            record = copy.deepcopy(record_template)
            record.update(
                recordId=record_id,
                sourceExecutionKey=f"execution-{index:06d}-{leg}",
                sourceEvidenceSnapshotId=f"snapshot-{index:06d}-{leg}-00",
                occurredAt=timestamp(opened if leg == 0 else closed),
                side="BUY" if leg == 0 else "SELL",
                price={
                    "value": "100" if leg == 0 else ("99" if pnl == "-1" else "101"),
                    "sourceScale": 0,
                },
                quantity={"value": "1", "sourceScale": 0},
            )
            record["recordHash"] = jcs_sha256(
                {key: value for key, value in record.items() if key != "recordHash"}
            )
            ledger["canonicalRecords"].append(record)
            for evidence in range(evidence_per_record):
                ledger["sourceEvidenceReferences"].append(
                    {
                        "snapshotId": f"snapshot-{index:06d}-{leg}-{evidence:02d}",
                        "recordId": record_id,
                        "coreArtifactId": f"synthetic-trades-{evidence:02d}",
                        "sourceRowNumber": index * 2 + leg + 1,
                        "sourceRowHash": jcs_sha256([record_id, evidence]),
                    }
                )
            allocations.append(
                {
                    "allocationId": allocation_id,
                    "recordId": record_id,
                    "reviewUnitId": unit_id,
                    "action": action,
                    "allocatedQuantity": {"value": "1", "sourceScale": 0},
                }
            )
            extras.append(
                {
                    "allocationId": allocation_id,
                    "evidenceRecordIds": [record_id],
                    "allocatedFee": money("0"),
                    "allocatedReportedPnl": money("0" if leg == 0 else pnl),
                }
            )
        unit = copy.deepcopy(unit_template)
        unit.update(
            reviewUnitId=unit_id,
            outcomeClassification="LOSS" if pnl == "-1" else "WIN",
            reconstructionStatus="COMPLETE",
            reconciliationStatus="EXACT",
            openedAt=timestamp(opened),
            closedAt=timestamp(closed),
            initialQuantity={"value": "1", "sourceScale": 0},
            netTradingPnl=money(pnl),
            allocations=allocations,
            capability=capabilities,
            exclusionReasons=[],
        )
        manifest["reviewUnits"].append(unit)
        ledger["reviewUnitEngineInputs"].append(
            {
                "reviewUnitId": unit_id,
                "evidenceRecordIds": record_ids,
                "stableEpisodeTimeAvailable": True,
                "allocationEvidence": extras,
            }
        )
    ledger_hash = jcs_sha256(
        [
            {key: value for key, value in record.items() if key != "recordHash"}
            for record in ledger["canonicalRecords"]
        ]
    )
    ledger["ledgerContentHash"] = manifest["canonicalRecordSetHash"] = ledger_hash
    manifest["counts"].update(
        canonicalRecordCount=episodes * 2,
        reviewUnitCount=episodes,
        sourceEvidenceSnapshotCount=episodes * 2 * evidence_per_record,
        positionAllocationCount=episodes * 2,
        excludedReviewUnitCount=0,
        capabilityCount=len(capabilities),
        validationIssueCount=0,
    )
    # Coverage describes synthetic sources, not real imported artifacts.
    for coverage in manifest["artifactCoverages"]:
        coverage.update(
            earliestObservedAt=timestamp(start),
            latestObservedAt=timestamp(closed),
            rowCount=episodes
            if coverage["artifactRole"] == "POSITION_HISTORY"
            else episodes * 2 * evidence_per_record,
        )
    manifest["manifestHash"] = jcs_sha256(
        {key: value for key, value in manifest.items() if key != "manifestHash"}
    )
    config = copy.deepcopy(vector["analysisConfig"])
    config["analysisPeriod"]["endExclusive"] = timestamp(closed + timedelta(hours=1))
    config_hash = analysis_config_hash(config)
    bodies = {ROLES[0]: canonical_bytes(ledger), ROLES[1]: canonical_bytes(manifest)}
    identity = copy.deepcopy(vector["inputIdentity"])
    identity.update(
        ledgerContentHash=ledger_hash,
        analysisConfigHash=config_hash,
        capabilitySnapshot=capabilities,
    )
    for role, body in bodies.items():
        identity[role].update(byteSize=len(body), sha256=hashlib.sha256(body).hexdigest())
    identity[ROLES[1]]["manifestHash"] = manifest["manifestHash"]
    request = {
        "coreLogicalJobId": "synthetic-logical-job",
        "coreAnalysisRunId": "synthetic-run",
        "ledgerRevisionId": identity["ledgerRevisionId"],
        "inputIdentity": identity,
        "targetVersionSet": identity["targetVersionSet"],
        "analysisConfig": config,
        "analysisConfigHash": config_hash,
        "capabilitySnapshot": capabilities,
        "expectedInputHash": analysis_input_hash(identity),
        "inputHashSpecificationVersion": "TRADING_ANALYTICS_INPUT_HASH_V1",
        "resultSchemaVersion": "TRADING_ANALYTICS_RESULT_SCHEMA_V1",
        "resultHashSpecificationVersion": "TRADING_ANALYTICS_RESULT_HASH_V1",
        "retryPolicyVersion": "ANALYTICS_RETRY_V1",
        "resultDeliveryPolicy": {
            "policyVersion": "ANALYTICS_RESULT_DELIVERY_V1",
            "transportMode": "SINGLE_RESOURCE",
            "maxPayloadBytes": 536870912,
        },
        "deletionGeneration": 0,
        "attemptToken": "synthetic-attempt",
        **{
            role: {
                **{
                    key: identity[role][key]
                    for key in ("coreReferenceId", "mediaType", "byteSize", "sha256")
                },
                "accessUri": f"https://synthetic.invalid/{role}",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
            for role in ROLES
        },
    }
    validate_create_identity(request)
    return request, bodies
