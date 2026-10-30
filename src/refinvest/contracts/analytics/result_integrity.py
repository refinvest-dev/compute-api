"""Analytics V1 section definitions and independent public-result digests."""

from __future__ import annotations

from refinvest.contracts.analytics.identity import (
    RESULT_HASH_SPEC,
    RESULT_SCHEMA,
    AnalyticsContractError,
    jcs_sha256,
)

SECTIONS = (
    "EVIDENCE_MEMBERSHIPS",
    "FINDINGS",
    "METRICS",
    "OBSERVATIONS",
    "OUTCOME_CLASSIFICATIONS",
    "RELATIONS",
)

SECTION_FIELDS = {
    "OUTCOME_CLASSIFICATIONS": (
        "outcomeClassifications",
        "outcomeClassificationCount",
        "reviewUnitId",
    ),
    "OBSERVATIONS": ("observations", "observationCount", "observationId"),
    "METRICS": ("metrics", "metricCount", "metricId"),
    "RELATIONS": ("relations", "relationCount", "relationId"),
    "EVIDENCE_MEMBERSHIPS": ("evidenceMembership", "evidenceMembershipCount", "membershipId"),
    "FINDINGS": ("findings", "findingCount", "findingId"),
}


def _sort(values: list[str]) -> list[str]:
    return sorted(set(values), key=lambda value: value.encode("utf-16-be"))


def finish_sections(
    sections: dict[str, list[dict[str, object]]],
    *,
    input_hash: str,
    config_hash: str,
    version_set: dict[str, object],
) -> dict[str, object]:
    """Hash the public structures; never trust supplied counts or expected vector hashes."""
    if set(sections) != set(SECTIONS) or len(sections["METRICS"]) != 3 or sections["FINDINGS"]:
        raise AnalyticsContractError("RESULT_INTEGRITY_FAILURE")
    metric_ids = {str(item["definitionId"]) for item in sections["METRICS"]}
    if metric_ids != {
        "ENTRY_WITHIN_WINDOW_AFTER_LOSS",
        "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS",
        "WIN_LOSS_HOLDING_DURATION_DIFFERENCE",
    }:
        raise AnalyticsContractError("RESULT_INTEGRITY_FAILURE")
    for metric in sections["METRICS"]:
        metric["metricHash"] = jcs_sha256(
            {key: value for key, value in metric.items() if key != "metricHash"}
        )
    counts: dict[str, int] = {}
    digests: list[dict[str, object]] = []
    for section_type in SECTIONS:
        _, count_field, identity = SECTION_FIELDS[section_type]
        items = sections[section_type]
        item_ids = [str(item[identity]) for item in items]
        if item_ids != _sort(item_ids):
            raise AnalyticsContractError("RESULT_INTEGRITY_FAILURE")
        counts[count_field] = len(items)
        digests.append(
            {
                "sectionType": section_type,
                "itemCount": len(items),
                "hashSpecificationVersion": RESULT_HASH_SPEC,
                "sectionHash": jcs_sha256(items),
            }
        )
    manifest: dict[str, object] = {
        "resultHashSpecificationVersion": RESULT_HASH_SPEC,
        "resultSchemaVersion": RESULT_SCHEMA,
        "inputHash": input_hash,
        "inputHashSpecificationVersion": "TRADING_ANALYTICS_INPUT_HASH_V1",
        "analysisConfigHash": config_hash,
        "targetVersionSet": version_set,
        "counts": counts,
        "sectionDigests": digests,
    }
    return {
        "counts": counts,
        "sectionDigests": digests,
        "evidenceHash": next(
            item["sectionHash"] for item in digests if item["sectionType"] == "EVIDENCE_MEMBERSHIPS"
        ),
        "resultDigestManifest": manifest,
        "resultHash": jcs_sha256(manifest),
    }
