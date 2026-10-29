"""Typed Analytics V1 projection of pure Engine results to public sections."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from refinvest.contracts.analytics.identity import AnalyticsContractError
from refinvest.contracts.analytics.result_integrity import SECTION_FIELDS, SECTIONS
from refinvest.trading_review.analytics.model import (
    Distribution,
    ExposureValues,
    HoldingValues,
    MetricResult,
    ReentryValues,
)
from refinvest.trading_review.decimal import ExactRational

REENTRY = "ENTRY_WITHIN_WINDOW_AFTER_LOSS"
EXPOSURE = "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS"
HOLDING = "WIN_LOSS_HOLDING_DURATION_DIFFERENCE"


def _sort(values: list[str]) -> list[str]:
    return sorted(set(values), key=lambda value: value.encode("utf-16-be"))


def _utc(value: datetime) -> str:
    utc = value.astimezone(UTC)
    fraction = f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise AnalyticsContractError("RESULT_INTEGRITY_FAILURE")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _rational(value: ExactRational | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {"numerator": value.numerator, "denominator": value.denominator}


def _distribution(value: Distribution | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "count": value.count,
        "minimum": _rational(value.minimum),
        "p25": _rational(value.p25),
        "median": _rational(value.median),
        "p75": _rational(value.p75),
        "maximum": _rational(value.maximum),
        "mean": _rational(value.mean),
    }


def _values(value: ReentryValues | ExposureValues | HoldingValues) -> dict[str, object]:
    if isinstance(value, ReentryValues):
        return {
            "eligibleLossCount": value.eligible_loss_count,
            "lossWithEntryCount": value.loss_with_entry_count,
            "relatedEntryCount": value.related_entry_count,
            "sameSymbolCount": value.same_symbol_count,
            "differentSymbolCount": value.different_symbol_count,
            "sameInstrumentRelatedEntryCount": value.same_instrument_related_entry_count,
            "differentInstrumentRelatedEntryCount": value.different_instrument_related_entry_count,
            "entryWithinWindowRate": _rational(value.entry_within_window_rate),
            "elapsedTimeDistribution": _distribution(value.elapsed_time_distribution),
        }
    if isinstance(value, ExposureValues):
        pairs = [
            {
                "lossId": pair.loss_id,
                "entryId": pair.entry_id,
                "currency": pair.currency,
                "lossNotional": _decimal(pair.loss_notional),
                "entryNotional": _decimal(pair.entry_notional),
                "absoluteDelta": _decimal(pair.absolute_delta),
                "ratio": _rational(pair.ratio),
                "percentageDelta": _rational(pair.percentage_delta),
                "sameInstrument": pair.same_instrument,
                "sameDirection": pair.same_direction,
                "baselineEpisodeIds": _sort(list(pair.baseline_episode_ids)),
                "baselineMedian": _rational(pair.baseline_median),
                "baselineRatio": _rational(pair.baseline_ratio),
                "baselineDelta": _rational(pair.baseline_delta),
                "baselineReason": pair.baseline_reason,
            }
            for pair in value.pairs
        ]
        pairs.sort(
            key=lambda item: (str(item["lossId"]), str(item["entryId"]), str(item["currency"]))
        )
        return {
            "pairCount": value.pair_count,
            "increasedCount": value.increased_count,
            "equalCount": value.equal_count,
            "decreasedCount": value.decreased_count,
            "medianNextEntryVsLossEpisodeRatio": _rational(
                value.median_next_entry_vs_loss_episode_ratio
            ),
            "ratioDistribution": _distribution(value.ratio_distribution),
            "percentageDeltaDistribution": _distribution(value.percentage_delta_distribution),
            "absoluteDeltaByCurrency": [
                {"currency": currency, "distribution": _distribution(distribution)}
                for currency, distribution in sorted(value.absolute_delta_by_currency)
            ],
            "pairs": pairs,
        }
    return {
        "winCount": value.win_count,
        "lossCount": value.loss_count,
        "breakevenCount": value.breakeven_count,
        "unavailableCount": value.unavailable_count,
        "winDistribution": _distribution(value.win_distribution),
        "lossDistribution": _distribution(value.loss_distribution),
        "medianDifferenceMicroseconds": _rational(value.median_difference_microseconds),
        "medianRatio": _rational(value.median_ratio),
        "medianRatioReason": value.median_ratio_reason,
    }


def _scalar_inputs(metric: MetricResult) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for unit_id, name, value in metric.scalar_inputs:
        text = value
        if name.endswith("At"):
            text = _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        result.append({"reviewUnitId": unit_id, "name": name, "value": text})
    result.sort(key=lambda item: (item["reviewUnitId"], item["name"], item["value"]))
    return result


def build_sections(
    results: tuple[MetricResult, MetricResult, MetricResult],
    ledger: dict[str, object],
    manifest: dict[str, object],
) -> dict[str, list[dict[str, object]]]:
    """Project pure Engine results to the six public, referentially joined sections."""
    sections: dict[str, list[dict[str, object]]] = {name: [] for name in SECTIONS}
    units = {
        str(unit["reviewUnitId"]): unit
        for unit in cast(list[dict[str, object]], manifest["reviewUnits"])
    }
    snapshots: dict[str, list[str]] = {}
    for row in cast(list[dict[str, object]], ledger["sourceEvidenceReferences"]):
        snapshots.setdefault(str(row["recordId"]), []).append(str(row["snapshotId"]))
    supplements = {
        str(row["reviewUnitId"]): row
        for row in cast(list[dict[str, object]], ledger["reviewUnitEngineInputs"])
    }
    memberships: dict[str, dict[str, object]] = {}

    def add_membership(
        metric_id: str, unit_id: str, role: str, reasons: tuple[str, ...] = ()
    ) -> str:
        reason = reasons[0] if role == "EXCLUDED" else ""
        membership_id = f"{metric_id}:{unit_id}:{role}:{reason}"
        if membership_id in memberships:
            return membership_id
        unit = units[unit_id]
        allocation_ids = _sort(
            [
                str(item["allocationId"])
                for item in cast(list[dict[str, object]], unit["allocations"])
            ]
        )
        supplement = supplements[unit_id]
        record_ids = _sort(
            [str(item) for item in cast(list[object], supplement["evidenceRecordIds"])]
            + [
                str(record_id)
                for item in cast(list[dict[str, object]], supplement["allocationEvidence"])
                for record_id in cast(list[object], item["evidenceRecordIds"])
            ]
        )
        memberships[membership_id] = {
            "membershipId": membership_id,
            "metricId": metric_id,
            "reviewUnitId": unit_id,
            "role": role,
            "exclusionReason": reason or None,
            "reasons": list(reasons) if role in {"EXCLUDED", "SELECTION_BOUNDARY"} else [],
            "allocationRecordIds": allocation_ids,
            "canonicalRecordIds": record_ids,
            "sourceEvidenceSnapshotIds": _sort(
                [snapshot for item in record_ids for snapshot in snapshots[item]]
            ),
        }
        return membership_id

    metric_members: dict[str, list[str]] = {}
    for result in results:
        baselines: set[str] = (
            {
                baseline_id
                for pair in result.values.pairs
                for baseline_id in pair.baseline_episode_ids
            }
            if result.definition_id == EXPOSURE and isinstance(result.values, ExposureValues)
            else set()
        )
        ids: list[str] = []
        for role, source in (
            ("SUBJECT", result.subject_membership),
            ("COMPARISON", result.comparison_membership),
            ("EXCLUDED", result.excluded_membership),
        ):
            for member in source:
                actual_role = (
                    "PRIOR_BASELINE"
                    if member.episode_id in baselines
                    else ("SELECTION_BOUNDARY" if member.role == "SELECTION_BOUNDARY" else role)
                )
                reasons = member.reasons if actual_role == "EXCLUDED" else ()
                for reason in reasons if reasons else (None,):
                    member_reasons = (
                        (reason,)
                        if reason is not None
                        else (member.reasons if actual_role == "SELECTION_BOUNDARY" else ())
                    )
                    ids.append(
                        add_membership(
                            result.definition_id, member.episode_id, actual_role, member_reasons
                        )
                    )
        if result.definition_id == REENTRY:
            for relation in result.relations:
                for candidate in relation.candidates:
                    ids.append(add_membership(REENTRY, candidate.entry_id, "RELATION_CANDIDATE"))
                    if candidate.entry_id in relation.related_entry_ids:
                        ids.append(add_membership(REENTRY, candidate.entry_id, "RELATED_ENTRY"))
        if result.definition_id == EXPOSURE and isinstance(result.values, ExposureValues):
            for pair in result.values.pairs:
                for baseline_id in pair.baseline_episode_ids:
                    ids.append(add_membership(EXPOSURE, baseline_id, "PRIOR_BASELINE"))
        metric_members[result.definition_id] = _sort(ids)
        sections["METRICS"].append(
            {
                "metricId": result.definition_id,
                "definitionId": result.definition_id,
                "definitionVersion": result.definition_version,
                "status": result.status.value,
                "findingEligibility": result.finding_eligibility.value,
                "requiredCapabilities": _sort(
                    [item.value for item in result.required_capabilities]
                ),
                "populationCount": result.population_count,
                "eligibleCount": result.eligible_count,
                "excludedCount": result.excluded_count,
                "excludedReasonCounts": dict(result.excluded_reason_counts),
                "scalarInputs": _scalar_inputs(result),
                "relationIds": _sort([f"{item.loss_id}:{REENTRY}" for item in result.relations])
                if result.definition_id in {REENTRY, EXPOSURE}
                else [],
                "membershipIds": metric_members[result.definition_id],
                "values": _values(result.values),
            }
        )
    reentry = next(result for result in results if result.definition_id == REENTRY)
    exposure = next(result for result in results if result.definition_id == EXPOSURE)
    holding = next(result for result in results if result.definition_id == HOLDING)
    for relation in reentry.relations:
        relation_id = f"{relation.loss_id}:{REENTRY}"
        candidates = [
            {
                "entryId": item.entry_id,
                "role": item.role,
                "openedAt": _utc(item.opened_at) if item.opened_at else None,
                "membershipId": add_membership(REENTRY, item.entry_id, "RELATION_CANDIDATE"),
            }
            for item in relation.candidates
        ]
        candidates.sort(
            key=lambda item: (
                item["openedAt"] is None,
                item["openedAt"] or "",
                item["entryId"],
                item["role"],
            )
        )
        related_ids = _sort(list(relation.related_entry_ids))
        member_ids = [add_membership(REENTRY, relation.loss_id, "SUBJECT")]
        for item in candidates:
            member_ids.append(str(item["membershipId"]))
        member_ids.extend(add_membership(REENTRY, item, "RELATED_ENTRY") for item in related_ids)
        sections["RELATIONS"].append(
            {
                "relationId": relation_id,
                "lossId": relation.loss_id,
                "closedAt": _utc(relation.closed_at),
                "candidates": candidates,
                "relatedEntryIds": related_ids,
                "elapsedMicroseconds": relation.elapsed_microseconds,
                "membershipIds": _sort(member_ids),
            }
        )
        sections["OBSERVATIONS"].append(
            {
                "observationId": f"{REENTRY}:{relation.loss_id}:{relation_id}",
                "definitionId": REENTRY,
                "definitionVersion": reentry.definition_version,
                "subjectReviewUnitIds": [relation.loss_id],
                "observedAt": _utc(relation.closed_at),
                "attributes": [
                    item
                    for item in _scalar_inputs(reentry)
                    if item["reviewUnitId"] == relation.loss_id
                ],
                "relationIds": [relation_id],
                "membershipIds": [add_membership(REENTRY, relation.loss_id, "SUBJECT")],
            }
        )
    if isinstance(exposure.values, ExposureValues):
        for pair in exposure.values.pairs:
            unit_ids = _sort([pair.loss_id, pair.entry_id])
            sections["OBSERVATIONS"].append(
                {
                    "observationId": f"{EXPOSURE}:{pair.loss_id}:{pair.entry_id}",
                    "definitionId": EXPOSURE,
                    "definitionVersion": exposure.definition_version,
                    "subjectReviewUnitIds": unit_ids,
                    "observedAt": _utc(
                        datetime.fromisoformat(
                            str(units[pair.entry_id]["openedAt"]).replace("Z", "+00:00")
                        )
                    ),
                    "attributes": [
                        item
                        for item in _scalar_inputs(exposure)
                        if item["reviewUnitId"] in unit_ids
                    ],
                    "relationIds": [f"{pair.loss_id}:{REENTRY}"],
                    "membershipIds": _sort(
                        [
                            add_membership(EXPOSURE, pair.loss_id, "SUBJECT"),
                            add_membership(EXPOSURE, pair.entry_id, "COMPARISON"),
                        ]
                    ),
                }
            )
    for member in (*holding.subject_membership, *holding.comparison_membership):
        unit_id = member.episode_id
        role = "SUBJECT" if member in holding.subject_membership else "COMPARISON"
        sections["OBSERVATIONS"].append(
            {
                "observationId": f"{HOLDING}:{unit_id}:0",
                "definitionId": HOLDING,
                "definitionVersion": holding.definition_version,
                "subjectReviewUnitIds": [unit_id],
                "observedAt": _utc(
                    datetime.fromisoformat(str(units[unit_id]["closedAt"]).replace("Z", "+00:00"))
                ),
                "attributes": [
                    item for item in _scalar_inputs(holding) if item["reviewUnitId"] == unit_id
                ],
                "relationIds": [],
                "membershipIds": [add_membership(HOLDING, unit_id, role)],
            }
        )
    outcomes: dict[str, dict[str, object]] = {}
    for result in results:
        for item in result.outcome_classifications:
            values = {
                "reviewUnitId": item.episode_id,
                "outcome": item.outcome.value,
                "netTradingPnl": _decimal(item.net_trading_pnl)
                if item.net_trading_pnl is not None
                else None,
                "settlementCurrency": item.settlement_currency,
                "allocatedReportedPnl": sorted(
                    [
                        {"recordId": record_id, "amount": _decimal(amount), "currency": currency}
                        for record_id, amount, currency in item.allocated_reported_pnl
                    ],
                    key=lambda row: (row["recordId"], row["amount"], row["currency"]),
                ),
                "allocatedFees": sorted(
                    [
                        {"recordId": record_id, "amount": _decimal(amount), "currency": currency}
                        for record_id, amount, currency in item.allocated_fees
                    ],
                    key=lambda row: (row["recordId"], row["amount"], row["currency"]),
                ),
            }
            previous = outcomes.get(item.episode_id)
            if (
                previous is not None
                and {key: value for key, value in previous.items() if key != "membershipId"}
                != values
            ):
                raise AnalyticsContractError("RESULT_ITEM_CONFLICT")
            available = _sort(
                [
                    key
                    for key, value in memberships.items()
                    if value["reviewUnitId"] == item.episode_id
                ]
            )
            if not available:
                raise AnalyticsContractError("RESULT_INTEGRITY_FAILURE")
            outcomes[item.episode_id] = {**values, "membershipId": available[0]}
    sections["OUTCOME_CLASSIFICATIONS"] = list(outcomes.values())
    sections["EVIDENCE_MEMBERSHIPS"] = list(memberships.values())
    for section_type, items in sections.items():
        identity = SECTION_FIELDS[section_type][2]
        items.sort(key=lambda item: str(item[identity]).encode("utf-16-be"))
        if len({str(item[identity]) for item in items}) != len(items):
            raise AnalyticsContractError("RESULT_ITEM_CONFLICT")
    return sections
