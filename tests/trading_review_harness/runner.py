from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.trading_review_harness.canonical import (
    canonical_json_bytes,
    content_hash,
    decimal_node,
    sha256_hex,
    timestamp_text,
)
from tests.trading_review_harness.model import (
    Allocation,
    Episode,
    Execution,
    FixtureResult,
    InstrumentTerms,
    JsonObject,
    PositionSummary,
)
from tests.trading_review_harness.parser import (
    POSITION_ROLE,
    TRADE_ROLE,
    ParseFailure,
    derive_trade_records,
    load_input,
    load_terms,
    parse_executions,
    parse_summaries,
)
from tests.trading_review_harness.reconcile import reconcile
from tests.trading_review_harness.reconstruct import reconstruct

PROVENANCE_REFERENCE_RE = re.compile(r"^([^:]+):r[0-9]+:([0-9a-f]{12})$")


def _stable_hash_tree(value: Any) -> Any:
    if isinstance(value, str):
        match = PROVENANCE_REFERENCE_RE.fullmatch(value)
        if match is not None:
            return f"{match.group(1)}:{match.group(2)}"
        if ":r" in value and value.startswith("BINANCE_USDS_POSITION_HISTORY_V1:"):
            parts = value.rsplit(":", maxsplit=1)
            return f"BINANCE_USDS_POSITION_HISTORY_V1:{parts[-1]}"
        return value
    if isinstance(value, list):
        normalized = [_stable_hash_tree(item) for item in value]
        if normalized and all(isinstance(item, str) for item in normalized):
            return sorted(normalized)
        return normalized
    if isinstance(value, dict):
        normalized = {
            key: _stable_hash_tree(item) for key, item in value.items() if key != "summarySourceRow"
        }
        matches = normalized.get("summaryMatches")
        if isinstance(matches, list):
            matches.sort(key=lambda item: str(item["summaryKey"]))
        return normalized
    return value


def _decimal_tree(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_node(value)
    if isinstance(value, dict):
        return {key: _decimal_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimal_tree(item) for item in value]
    if isinstance(value, set):
        return sorted(_decimal_tree(item) for item in value)
    return value


def _terms_output(terms_by_symbol: dict[str, InstrumentTerms]) -> list[JsonObject]:
    return [
        {
            "symbol": terms.symbol,
            "stepSize": decimal_node(terms.step_size),
            "tickSize": decimal_node(terms.tick_size),
            "contractMultiplier": decimal_node(terms.contract_multiplier),
            "settlementAsset": terms.settlement_asset,
            "settlementPrecision": terms.settlement_precision,
        }
        for terms in sorted(terms_by_symbol.values(), key=lambda item: item.symbol)
    ]


def _episode_output(episode: Episode) -> JsonObject:
    return {
        "deterministicKey": episode.key,
        "symbol": episode.symbol,
        "direction": episode.direction,
        "openedAt": timestamp_text(episode.opened_at),
        "closedAt": timestamp_text(episode.closed_at),
        "initialQuantity": decimal_node(episode.initial_quantity),
        "maxQuantity": decimal_node(episode.max_quantity),
        "openingVolume": decimal_node(episode.opening_volume),
        "closingVolume": decimal_node(episode.closing_volume),
        "averageEntry": (
            {
                "numerator": decimal_node(episode.entry_numerator),
                "denominator": decimal_node(episode.entry_denominator),
            }
            if episode.entry_numerator is not None and episode.entry_denominator is not None
            else None
        ),
        "averageExit": (
            {
                "numerator": decimal_node(episode.exit_numerator),
                "denominator": decimal_node(episode.exit_denominator),
            }
            if episode.exit_numerator is not None and episode.exit_denominator is not None
            else None
        ),
        "grossTradingPnl": decimal_node(episode.gross_pnl),
        "tradingFeesByAsset": {
            asset: decimal_node(value) for asset, value in sorted(episode.fees.items())
        },
        "netTradingPnl": decimal_node(episode.net_pnl),
        "reconstructionStatus": episode.reconstruction_status,
        "reconciliationStatus": episode.reconciliation_status,
        "capabilities": sorted(episode.capabilities),
        "eligible": episode.eligible,
        "exclusionReasons": episode.exclusion_reasons,
        "provenance": [item.reference() for item in episode.provenance],
    }


def _allocation_output(allocation: Allocation) -> JsonObject:
    return {
        "executionKey": allocation.execution_key,
        "episodeKey": allocation.episode_key,
        "action": allocation.action,
        "allocatedQuantity": decimal_node(allocation.quantity),
        "feeByAsset": {allocation.fee_asset: decimal_node(allocation.fee)},
        "allocatedReportedPnl": decimal_node(allocation.reported_pnl),
        "reversalGroupId": allocation.reversal_group_id,
        "provenance": [item.reference() for item in allocation.provenance],
    }


def invariant_errors(
    executions: list[Execution],
    episodes: list[Episode],
    allocations: list[Allocation],
    summary_matches: list[JsonObject],
) -> list[str]:
    errors: list[str] = []
    allocations_by_execution: dict[str, list[Allocation]] = defaultdict(list)
    for allocation in allocations:
        allocations_by_execution[allocation.execution_key].append(allocation)
    for execution in executions:
        linked = allocations_by_execution[execution.key]
        if sum((item.quantity for item in linked), Decimal(0)) != execution.quantity:
            errors.append(f"EXECUTION_QUANTITY_CONSERVATION:{execution.key}")
        if sum((item.fee for item in linked), Decimal(0)) != execution.fee:
            errors.append(f"FEE_CONSERVATION:{execution.key}:{execution.fee_asset}")
        if sum((item.reported_pnl for item in linked), Decimal(0)) != execution.realized_pnl:
            errors.append(f"REPORTED_PNL_CONSERVATION:{execution.key}")
        reversal = [item for item in linked if item.reversal_group_id is not None]
        if len(linked) > 1 and not reversal:
            errors.append(f"DUPLICATE_EXECUTION_ASSIGNMENT:{execution.key}")
        if reversal and (
            len(reversal) != 2
            or {item.action for item in reversal} != {"REVERSAL_CLOSE", "REVERSAL_OPEN"}
            or len({item.episode_key for item in reversal}) != 2
        ):
            errors.append(f"REVERSAL_DISJOINT_SPLIT:{execution.key}")
    episode_keys = {episode.key for episode in episodes}
    for allocation in allocations:
        if allocation.episode_key not in episode_keys:
            errors.append(f"UNKNOWN_EPISODE_ALLOCATION:{allocation.execution_key}")
        if not allocation.provenance:
            errors.append(f"MISSING_PROVENANCE:{allocation.execution_key}")
    for episode in episodes:
        if (
            episode.reconstruction_status == "COMPLETE"
            and episode.opening_volume != episode.closing_volume
        ):
            errors.append(f"COMPLETE_VOLUME_CONSERVATION:{episode.key}")
        if episode.reconstruction_status != "COMPLETE" and episode.eligible:
            errors.append(f"INELIGIBLE_RECONSTRUCTION_PROMOTION:{episode.key}")
        if episode.net_pnl is None and "CLOSED_OUTCOME" in episode.capabilities:
            errors.append(f"MISSING_CAPABILITY_AS_ZERO:{episode.key}")
        if not episode.provenance:
            errors.append(f"MISSING_PROVENANCE:{episode.key}")
    assigned = [
        match["summaryKey"]
        for match in summary_matches
        if match.get("episodeKey") is not None
        and match.get("status") in {"EXACT", "WITHIN_ROUNDING_TOLERANCE"}
    ]
    if len(assigned) != len(set(assigned)):
        errors.append("SUMMARY_ASSIGNED_TO_MULTIPLE_ELIGIBLE_EPISODES")
    for match in summary_matches:
        if match.get("status") == "AMBIGUOUS" and match.get("episodeKey") is not None:
            errors.append(f"AMBIGUOUS_HEURISTIC_SELECTION:{match['summaryKey']}")
        if not match.get("provenance"):
            errors.append(f"MISSING_PROVENANCE:{match['summaryKey']}")
    return sorted(errors)


def _conservation_output(
    executions: list[Execution],
    episodes: list[Episode],
    allocations: list[Allocation],
    summary_matches: list[JsonObject],
) -> JsonObject:
    errors = invariant_errors(executions, episodes, allocations, summary_matches)
    source_fees: dict[str, Decimal] = defaultdict(Decimal)
    allocated_fees: dict[str, Decimal] = defaultdict(Decimal)
    for execution in executions:
        source_fees[execution.fee_asset] += execution.fee
    for allocation in allocations:
        allocated_fees[allocation.fee_asset] += allocation.fee
    source_pnl = sum((item.realized_pnl for item in executions), Decimal(0))
    allocated_pnl = sum((item.reported_pnl for item in allocations), Decimal(0))
    return {
        "quantity": not any(error.startswith("EXECUTION_QUANTITY") for error in errors),
        "completeEpisodeVolumes": not any(error.startswith("COMPLETE_VOLUME") for error in errors),
        "feesByAsset": {
            asset: {
                "source": decimal_node(source_fees[asset]),
                "allocated": decimal_node(allocated_fees[asset]),
                "equal": source_fees[asset] == allocated_fees[asset],
            }
            for asset in sorted(set(source_fees) | set(allocated_fees))
        },
        "reportedPnl": {
            "source": decimal_node(source_pnl),
            "allocated": decimal_node(allocated_pnl),
            "equal": source_pnl == allocated_pnl,
        },
        "errors": errors,
        "passed": not errors,
    }


def _has_unsupported_overlap(summaries: list[PositionSummary]) -> bool:
    minute = timedelta(minutes=1)
    for left_index, left in enumerate(summaries):
        if left.status != "Closed":
            continue
        for right in summaries[left_index + 1 :]:
            if (
                right.status != "Closed"
                or left.symbol != right.symbol
                or left.direction == right.direction
            ):
                continue
            overlap_start = max(left.opened_minute, right.opened_minute)
            overlap_end = min(left.closed_minute + minute, right.closed_minute + minute)
            if overlap_end - overlap_start > minute:
                return True
    return False


def _rejected_result(fixture_path: Path, scenario_id: str, code: str) -> FixtureResult:
    return FixtureResult(
        fixture_path=fixture_path,
        actual={
            "scenarioId": scenario_id,
            "import": {"accepted": False, "failureCode": code, "warnings": []},
            "instrumentTerms": [],
            "orderedExecutionKeysByInstrument": {},
            "episodes": [],
            "allocations": [],
            "summaryMatches": [],
            "conservation": None,
            "manifest": None,
        },
        canonical_bytes=None,
        manifest_hash=None,
    )


def run_fixture(fixture_path: Path, *, reverse_processing_order: bool = False) -> FixtureResult:
    config = load_input(fixture_path / "input.json")
    scenario_id = str(config.get("scenarioId", fixture_path.name))
    try:
        terms_by_symbol = load_terms(config)
        executions, trade_hash = parse_executions(fixture_path, config, terms_by_symbol)
        summaries, position_hash = parse_summaries(fixture_path, config, terms_by_symbol)
    except ParseFailure as exc:
        return _rejected_result(fixture_path, scenario_id, exc.failure.code)
    if _has_unsupported_overlap(summaries):
        return _rejected_result(fixture_path, scenario_id, "UNSUPPORTED_POSITION_MODE")
    if reverse_processing_order:
        executions = list(reversed(executions))
        summaries = list(reversed(summaries))
    fee_records, pnl_records = derive_trade_records(executions, terms_by_symbol)
    policy_version = str(config["reconstructionPolicyVersion"])
    raw_valuations = config.get("feeValuations", {})
    fee_valuations = {
        str(asset): Decimal(str(value)) for asset, value in dict(raw_valuations).items()
    }
    episodes, allocations = reconstruct(executions, terms_by_symbol, policy_version, fee_valuations)
    episodes, summary_matches = reconcile(episodes, summaries, terms_by_symbol)
    warning_codes = sorted(
        {
            "RECONCILIATION_WITHIN_ROUNDING_TOLERANCE"
            for episode in episodes
            if episode.reconciliation_status == "WITHIN_ROUNDING_TOLERANCE"
        }
    )
    conservation = _conservation_output(executions, episodes, allocations, summary_matches)
    ordered_keys: dict[str, list[str]] = defaultdict(list)
    for execution in sorted(
        executions, key=lambda item: (item.symbol, item.occurred_at, int(item.raw_trade_id))
    ):
        ordered_keys[execution.symbol].append(
            f"{timestamp_text(execution.occurred_at)}|{execution.raw_trade_id}"
        )
    manifest_payload: JsonObject = {
        "manifestSchemaVersion": str(config["manifestSchemaVersion"]),
        "normalizerVersion": str(config["normalizerVersion"]),
        "reconstructionPolicyVersion": policy_version,
        "instrumentTerms": _terms_output(terms_by_symbol),
        "sourceArtifacts": [
            {"role": TRADE_ROLE, "contentHash": trade_hash},
            {"role": POSITION_ROLE, "contentHash": position_hash},
        ],
        "canonicalRecordsHash": content_hash(
            {
                "executions": [
                    {
                        "key": execution.key,
                        "rowHashes": sorted(item.row_hash for item in execution.provenance),
                    }
                    for execution in sorted(executions, key=lambda x: x.key)
                ],
                "fees": [
                    {
                        "asset": record.asset,
                        "balanceEffect": decimal_node(record.balance_effect),
                        "executionKey": record.execution_key,
                        "kind": record.kind,
                        "sourceAmount": decimal_node(record.source_amount),
                    }
                    for record in sorted(fee_records, key=lambda item: item.execution_key)
                ],
                "reportedPnl": [
                    {
                        "asset": record.asset,
                        "executionKey": record.execution_key,
                        "realizedProfit": decimal_node(record.realized_profit),
                    }
                    for record in sorted(pnl_records, key=lambda item: item.execution_key)
                ],
                "summaries": sorted(summary.provenance.row_hash for summary in summaries),
            }
        ),
        "orderedExecutionKeysByInstrument": dict(sorted(ordered_keys.items())),
        "episodes": [_episode_output(item) for item in episodes],
        "allocations": [_allocation_output(item) for item in allocations],
        "summaryMatches": _decimal_tree(summary_matches),
        "conservation": conservation,
        "issues": sorted(
            {
                *warning_codes,
                *(reason for episode in episodes for reason in episode.exclusion_reasons),
            }
        ),
    }
    canonical_bytes = canonical_json_bytes(_stable_hash_tree(manifest_payload))
    manifest_hash = sha256_hex(canonical_bytes)
    actual: JsonObject = {
        "scenarioId": scenario_id,
        "import": {"accepted": True, "failureCode": None, "warnings": warning_codes},
        "instrumentTerms": _terms_output(terms_by_symbol),
        "orderedExecutionKeysByInstrument": dict(sorted(ordered_keys.items())),
        "episodes": deepcopy(manifest_payload["episodes"]),
        "allocations": deepcopy(manifest_payload["allocations"]),
        "summaryMatches": deepcopy(manifest_payload["summaryMatches"]),
        "conservation": conservation,
        "manifest": {
            "schemaVersion": str(config["manifestSchemaVersion"]),
            "normalizerVersion": str(config["normalizerVersion"]),
            "reconstructionPolicyVersion": policy_version,
            "canonicalHash": manifest_hash,
        },
    }
    return FixtureResult(fixture_path, actual, canonical_bytes, manifest_hash)
