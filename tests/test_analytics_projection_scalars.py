"""Characterize public scalar rows before optimizing Analytics projection."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from typing import Any

from refinvest.application.analytics_result import bounded_analytics_json, build_sections
from tests.test_trading_review_analytics import episode, run


def projection_case() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Pure Engine outputs plus only the normalized evidence fields projection reads."""
    items = [
        episode("prior-1", opened="09:00", closed="09:02", outcome="WIN"),
        episode("prior-2", opened="09:04", closed="09:06", outcome="WIN"),
        episode("prior-3", opened="09:08", closed="09:10", outcome="WIN"),
        episode("\ue000", opened="09:12", closed="09:13"),
        episode("\U0001f600", opened="09:14", closed="09:15", outcome="WIN"),
        episode("loss-2", opened="09:16", closed="09:17"),
        episode("entry-2", opened="09:18", closed="09:19", outcome="WIN"),
        episode("late", opened="10:20", closed="10:21", outcome="WIN"),
    ]
    metrics = run(*items)
    ledger: dict[str, Any] = {"sourceEvidenceReferences": [], "reviewUnitEngineInputs": []}
    units = []
    for unit, records in items:
        allocations = [
            {"allocationId": f"allocation-{unit.episode_id}-{index}"}
            for index in range(len(unit.allocations))
        ]
        units.append(
            {
                "reviewUnitId": unit.episode_id,
                "openedAt": unit.opened_at.isoformat(),
                "closedAt": unit.closed_at.isoformat(),
                "allocations": allocations,
            }
        )
        ledger["reviewUnitEngineInputs"].append(
            {
                "reviewUnitId": unit.episode_id,
                "evidenceRecordIds": list(unit.evidence_record_ids),
                "allocationEvidence": [
                    {"evidenceRecordIds": list(allocation.evidence_record_ids)}
                    for allocation in unit.allocations
                ],
            }
        )
        ledger["sourceEvidenceReferences"].extend(
            {"recordId": record.record_id, "snapshotId": f"snapshot-{record.record_id}"}
            for record in records
        )
    return metrics, ledger, {"reviewUnits": units}


def test_complete_public_projection_matches_preoptimization_bytes() -> None:
    metrics, ledger, manifest = projection_case()
    assert all(metric.status.value == "AVAILABLE" for metric in metrics)
    sections = build_sections(metrics, ledger, manifest)
    body = bounded_analytics_json(sections, 1_000_000)
    assert hashlib.sha256(body).hexdigest() == (
        "49537f0c105f62aaaeeff2e65b2a5803d78289a6dcfa29381824d1d2de713653"
    )
    assert body == bounded_analytics_json(build_sections(metrics, ledger, manifest), 1_000_000)
    assert any(
        candidate["role"] == "AFTER_WINDOW"
        for relation in sections["RELATIONS"]
        for candidate in relation["candidates"]
    )


def test_observation_attributes_preserve_scalar_order_multiplicity_and_utc() -> None:
    metrics, ledger, manifest = projection_case()
    extra = (
        ("\U0001f600", "openedAt", "2026-01-01T18:14:00+09:00"),
        ("\ue000", "synthetic", "z"),
        ("\ue000", "synthetic", "a"),
        ("\ue000", "synthetic", "a"),
    )
    modified = tuple(
        replace(metric, scalar_inputs=(*metric.scalar_inputs, *extra)) for metric in metrics
    )
    sections = build_sections(modified, ledger, manifest)
    by_metric = {metric["metricId"]: metric for metric in sections["METRICS"]}
    seen_multi_unit = False
    for observation in sections["OBSERVATIONS"]:
        rows = observation["attributes"]
        expected = [
            row
            for row in by_metric[observation["definitionId"]]["scalarInputs"]
            if row["reviewUnitId"] in observation["subjectReviewUnitIds"]
        ]
        assert rows == expected
        if set(observation["subjectReviewUnitIds"]) == {"\ue000", "\U0001f600"}:
            seen_multi_unit = True
            # Python scalar order differs from the UTF-16 order of subject IDs.
            assert observation["subjectReviewUnitIds"] == ["\U0001f600", "\ue000"]
            assert rows[0]["reviewUnitId"] == "\ue000"
        if "\ue000" in observation["subjectReviewUnitIds"]:
            assert [row["value"] for row in rows if row["name"] == "synthetic"] == ["a", "a", "z"]
        assert all(
            row["value"] == "2026-01-01T09:14:00Z"
            for row in rows
            if row["reviewUnitId"] == "\U0001f600" and row["name"] == "openedAt"
        )
    assert seen_multi_unit


def test_projection_rows_do_not_alias_metrics_observations_or_later_calls() -> None:
    metrics, ledger, manifest = projection_case()
    input_before = copy.deepcopy((metrics, ledger, manifest))
    sections = build_sections(metrics, ledger, manifest)
    pristine = copy.deepcopy(sections)
    observation = next(row for row in sections["OBSERVATIONS"] if row["attributes"])
    observation["attributes"][0]["value"] = "mutated-by-caller"
    for current, original in zip(sections["OBSERVATIONS"], pristine["OBSERVATIONS"], strict=True):
        if current is not observation:
            assert current == original
    assert sections["METRICS"] == pristine["METRICS"]
    assert build_sections(metrics, ledger, manifest) == pristine
    assert (metrics, ledger, manifest) == input_before
