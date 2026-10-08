from __future__ import annotations

import json
import shutil
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from tests.trading_review_harness import run_fixture
from tests.trading_review_harness.parser import (
    derive_trade_records,
    load_input,
    load_terms,
    parse_executions,
    parse_summaries,
)
from tests.trading_review_harness.reconcile import reconcile
from tests.trading_review_harness.reconstruct import reconstruct
from tests.trading_review_harness.runner import invariant_errors

pytestmark = pytest.mark.skip(
    reason="legacy test-only PoC retired; production engine regression tests supersede it"
)

FIXTURE_ROOT = Path("tests/fixtures/trading_review/binance_usds_one_way/v1")
FIXTURES = sorted(path for path in FIXTURE_ROOT.iterdir() if path.is_dir())
SPEC_CONFLICT = "scenario-16-same-minute-multiple-episodes"


def _load_expected(path: Path) -> dict[str, object]:
    with (path / "expected.json").open("r", encoding="utf-8") as source:
        value = json.load(source)
    assert isinstance(value, dict)
    return value


def _internal_result(path: Path):  # type: ignore[no-untyped-def]
    config = load_input(path / "input.json")
    terms = load_terms(config)
    executions, _ = parse_executions(path, config, terms)
    summaries, _ = parse_summaries(path, config, terms)
    episodes, allocations = reconstruct(
        executions,
        terms,
        str(config["reconstructionPolicyVersion"]),
        {},
    )
    episodes, matches = reconcile(episodes, summaries, terms)
    return executions, episodes, allocations, matches


def test_discovers_exactly_twenty_five_normative_scenarios() -> None:
    assert len(FIXTURES) == 25
    assert [path.name.split("-", maxsplit=2)[1] for path in FIXTURES] == [
        f"{number:02d}" for number in range(1, 26)
    ]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda path: path.name)
def test_golden_expected_result(fixture: Path) -> None:
    actual = run_fixture(fixture).actual
    expected = _load_expected(fixture)
    if fixture.name == SPEC_CONFLICT:
        conflict = expected["knownSpecificationConflict"]
        assert isinstance(conflict, dict)
        documented = conflict["documentedExpected"]
        assert isinstance(documented, dict)
        # This assertion deliberately records the unresolved normative contradiction.
        pytest.xfail(str(conflict["reason"]))
    assert actual == expected


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda path: path.name)
def test_fixture_metadata_maps_all_domain_invariants(fixture: Path) -> None:
    config = load_input(fixture / "input.json")
    assert config["trInvariants"] == [f"TR-I{number:02d}" for number in range(1, 14)]


def test_scenario_16_conflict_is_machine_visible_and_not_silently_reinterpreted() -> None:
    expected = _load_expected(FIXTURE_ROOT / SPEC_CONFLICT)
    conflict = expected["knownSpecificationConflict"]
    assert isinstance(conflict, dict)
    actual = run_fixture(FIXTURE_ROOT / SPEC_CONFLICT).actual
    assert len(actual["episodes"]) == 1
    assert conflict["actualOrderedExecutions"] == ["1601", "1603", "1602", "1604"]
    assert conflict["documentedExpected"] == {
        "episodeCount": 2,
        "reconstructionStatus": ["COMPLETE", "COMPLETE"],
        "reconciliationStatus": ["EXACT", "EXACT"],
        "eligible": [True, True],
    }


@pytest.mark.parametrize(
    ("scenario", "accepted", "failure_code"),
    [
        ("scenario-01-simple-long", True, None),
        ("scenario-12-invalid-trade-id", False, "FILE_FORMAT_INVALID"),
        ("scenario-18-conflicting-content", False, "RECORD_CONFLICT"),
        ("scenario-21-hedge-overlap", False, "UNSUPPORTED_POSITION_MODE"),
    ],
)
def test_import_outcomes(scenario: str, accepted: bool, failure_code: str | None) -> None:
    result = run_fixture(FIXTURE_ROOT / scenario).actual
    assert result["import"] == {
        "accepted": accepted,
        "failureCode": failure_code,
        "warnings": [],
    }
    assert (result["manifest"] is not None) is accepted


@pytest.mark.parametrize(
    ("scenario", "reconstruction", "reconciliation", "eligible"),
    [
        ("scenario-01-simple-long", "COMPLETE", "EXACT", True),
        ("scenario-02-simple-short", "COMPLETE", "EXACT", True),
        ("scenario-09-left-censored", "INCONSISTENT", "INCOMPLETE_TRADES", False),
        ("scenario-10-right-censored", "RIGHT_CENSORED", "INCOMPLETE_TRADES", False),
        ("scenario-13-missing-execution", "INCONSISTENT", "INCOMPLETE_TRADES", False),
        ("scenario-14-trade-only", "COMPLETE", "SUMMARY_MISMATCH", False),
        ("scenario-15-summary-only", "INCONSISTENT", "INCOMPLETE_TRADES", False),
        ("scenario-22-unsupported-position-status", "COMPLETE", "SUMMARY_MISMATCH", False),
        ("scenario-23-position-pnl-mismatch", "COMPLETE", "SUMMARY_MISMATCH", False),
        (
            "scenario-24-rounding-within-tolerance",
            "COMPLETE",
            "WITHIN_ROUNDING_TOLERANCE",
            True,
        ),
        ("scenario-25-rounding-outside-tolerance", "COMPLETE", "SUMMARY_MISMATCH", False),
    ],
)
def test_normative_status_contract(
    scenario: str, reconstruction: str, reconciliation: str, eligible: bool
) -> None:
    episode = run_fixture(FIXTURE_ROOT / scenario).actual["episodes"][0]
    assert episode["reconstructionStatus"] == reconstruction
    assert episode["reconciliationStatus"] == reconciliation
    assert episode["eligible"] is eligible


@pytest.mark.parametrize(
    "scenario",
    [
        "scenario-07-long-to-short-reversal",
        "scenario-08-short-to-long-reversal",
    ],
)
def test_reversal_is_a_disjoint_quantity_fee_and_pnl_split(scenario: str) -> None:
    actual = run_fixture(FIXTURE_ROOT / scenario).actual
    reversal = [
        allocation
        for allocation in actual["allocations"]
        if allocation["reversalGroupId"] is not None
    ]
    assert [allocation["action"] for allocation in reversal] == [
        "REVERSAL_CLOSE",
        "REVERSAL_OPEN",
    ]
    assert reversal[1]["allocatedReportedPnl"] == {"unscaled": "0", "scale": 0}
    assert reversal[0]["reversalGroupId"] == reversal[1]["reversalGroupId"]
    expected_fees = (
        [{"unscaled": "3", "scale": 2}, {"unscaled": "2", "scale": 2}]
        if scenario.startswith("scenario-07")
        else [{"unscaled": "4", "scale": 2}, {"unscaled": "2", "scale": 2}]
    )
    assert [item["feeByAsset"]["USDT"] for item in reversal] == expected_fees
    assert actual["conservation"]["passed"] is True


def test_partial_entry_and_exit_keep_exact_rational_averages() -> None:
    entry = run_fixture(FIXTURE_ROOT / "scenario-03-split-long-entry").actual["episodes"][0]
    assert entry["averageEntry"] == {
        "numerator": {"unscaled": "36", "scale": 0},
        "denominator": {"unscaled": "3", "scale": 0},
    }
    exit_episode = run_fixture(FIXTURE_ROOT / "scenario-04-partial-close").actual["episodes"][0]
    assert exit_episode["averageExit"] == {
        "numerator": {"unscaled": "47", "scale": 0},
        "denominator": {"unscaled": "4", "scale": 0},
    }


def test_identical_execution_rows_deduplicate_and_merge_provenance() -> None:
    actual = run_fixture(FIXTURE_ROOT / "scenario-17-duplicate-row").actual
    assert len(actual["allocations"]) == 2
    assert len(actual["allocations"][0]["provenance"]) == 2


def test_each_trade_row_creates_fee_and_venue_reported_pnl_records() -> None:
    fixture = FIXTURE_ROOT / "scenario-19-fee-rebate"
    config = load_input(fixture / "input.json")
    terms = load_terms(config)
    executions, _ = parse_executions(fixture, config, terms)
    fees, pnl_records = derive_trade_records(executions, terms)
    assert [record.kind for record in fees] == ["COST", "REBATE"]
    assert [record.balance_effect for record in fees] == [Decimal("-0.02"), Decimal("0.01")]
    assert [record.realized_profit for record in pnl_records] == [Decimal("0"), Decimal("2")]
    assert all(record.asset == "USDT" for record in pnl_records)


def test_same_timestamp_orders_by_arbitrary_precision_trade_id() -> None:
    actual = run_fixture(FIXTURE_ROOT / "scenario-11-same-timestamp").actual
    ordered = actual["orderedExecutionKeysByInstrument"]["SYNTHUSDT"]
    assert [item.rsplit("|", maxsplit=1)[1] for item in ordered] == ["301", "302"]
    assert [item["action"] for item in actual["allocations"]] == ["OPEN", "REDUCE"]


def test_rebate_and_unvalued_fee_asset_keep_zero_distinct_from_unknown() -> None:
    rebate = run_fixture(FIXTURE_ROOT / "scenario-19-fee-rebate").actual["episodes"][0]
    assert rebate["tradingFeesByAsset"]["USDT"] == {"unscaled": "1", "scale": 2}
    assert rebate["netTradingPnl"] == {"unscaled": "199", "scale": 2}
    unvalued = run_fixture(FIXTURE_ROOT / "scenario-20-different-fee-asset").actual["episodes"][0]
    assert unvalued["tradingFeesByAsset"]["FEECOIN"] == {
        "unscaled": "1",
        "scale": 3,
    }
    assert unvalued["netTradingPnl"] is None
    assert "CLOSED_OUTCOME" not in unvalued["capabilities"]
    assert unvalued["eligible"] is True


def test_rounding_tolerance_is_derived_from_both_source_scales() -> None:
    actual = run_fixture(FIXTURE_ROOT / "scenario-24-rounding-within-tolerance").actual
    comparison = actual["summaryMatches"][0]["evaluatedPairs"][0]
    assert comparison["deltas"]["grossTradingPnl"] == {"unscaled": "4", "scale": 3}
    assert comparison["tolerances"]["grossTradingPnl"] == {
        "unscaled": "55",
        "scale": 4,
    }


def test_csv_never_grants_execution_origin_known() -> None:
    for fixture in FIXTURES:
        for episode in run_fixture(fixture).actual["episodes"]:
            assert "EXECUTION_ORIGIN_KNOWN" not in episode["capabilities"]


def test_status_values_are_limited_to_the_normative_enums() -> None:
    reconstruction = {"COMPLETE", "LEFT_CENSORED", "RIGHT_CENSORED", "INCONSISTENT", "UNSUPPORTED"}
    reconciliation = {
        "EXACT",
        "WITHIN_ROUNDING_TOLERANCE",
        "AMBIGUOUS",
        "INCOMPLETE_TRADES",
        "SUMMARY_MISMATCH",
        "UNSUPPORTED_OVERLAP",
    }
    for fixture in FIXTURES:
        actual = run_fixture(fixture).actual
        assert all(
            episode["reconstructionStatus"] in reconstruction
            and episode["reconciliationStatus"] in reconciliation
            for episode in actual["episodes"]
        )


def test_deterministic_replay_and_shuffled_processing_order() -> None:
    fixture = FIXTURE_ROOT / "scenario-07-long-to-short-reversal"
    first = run_fixture(fixture)
    second = run_fixture(fixture)
    shuffled = run_fixture(fixture, reverse_processing_order=True)
    assert first.manifest_hash == second.manifest_hash == shuffled.manifest_hash
    assert first.canonical_bytes == second.canonical_bytes == shuffled.canonical_bytes


def test_raw_csv_row_reordering_keeps_semantic_manifest_hash(tmp_path: Path) -> None:
    source = FIXTURE_ROOT / "scenario-07-long-to-short-reversal"
    reordered = tmp_path / "reordered"
    shutil.copytree(source, reordered)
    for filename in ("trade_history.csv", "position_history.csv"):
        path = reordered / filename
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0], *reversed(lines[1:])]) + "\n", encoding="utf-8")
    assert run_fixture(source).manifest_hash == run_fixture(reordered).manifest_hash


def test_different_symbol_processing_order_does_not_change_hash(tmp_path: Path) -> None:
    fixture = tmp_path / "two-symbols"
    shutil.copytree(FIXTURE_ROOT / "scenario-01-simple-long", fixture)
    config = load_input(fixture / "input.json")
    alternate_terms = deepcopy(config["instrumentTerms"][0])
    alternate_terms["symbol"] = "ALTSYNTHUSDT"
    config["instrumentTerms"].append(alternate_terms)
    (fixture / "input.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    trade_path = fixture / "trade_history.csv"
    trade_lines = trade_path.read_text(encoding="utf-8").splitlines()
    alternate_trades = [
        trade_lines[1].replace("SYNTHUSDT", "ALTSYNTHUSDT").replace(",101,", ",9101,"),
        trade_lines[2].replace("SYNTHUSDT", "ALTSYNTHUSDT").replace(",102,", ",9102,"),
    ]
    trade_path.write_text("\n".join([*trade_lines, *alternate_trades]) + "\n", encoding="utf-8")
    position_path = fixture / "position_history.csv"
    position_lines = position_path.read_text(encoding="utf-8").splitlines()
    alternate_position = position_lines[1].replace("SYNTHUSDT", "ALTSYNTHUSDT")
    position_path.write_text(
        "\n".join([*position_lines, alternate_position]) + "\n", encoding="utf-8"
    )
    normal = run_fixture(fixture)
    reversed_processing = run_fixture(fixture, reverse_processing_order=True)
    assert list(normal.actual["orderedExecutionKeysByInstrument"]) == [
        "ALTSYNTHUSDT",
        "SYNTHUSDT",
    ]
    assert normal.manifest_hash == reversed_processing.manifest_hash


def test_runtime_metadata_is_not_in_canonical_manifest() -> None:
    result = run_fixture(FIXTURE_ROOT / "scenario-01-simple-long")
    assert result.canonical_bytes is not None
    for forbidden in (b"generatedAt", b"runtimeId", b"worker", b"storagePath"):
        assert forbidden not in result.canonical_bytes


def test_version_and_pinned_terms_change_manifest_hash(tmp_path: Path) -> None:
    source = FIXTURE_ROOT / "scenario-01-simple-long"
    baseline = run_fixture(source).manifest_hash
    policy_fixture = tmp_path / "policy"
    terms_fixture = tmp_path / "terms"
    shutil.copytree(source, policy_fixture)
    shutil.copytree(source, terms_fixture)
    policy = load_input(policy_fixture / "input.json")
    policy["reconstructionPolicyVersion"] = "binance-usds-one-way-reconstruction-v2"
    (policy_fixture / "input.json").write_text(
        json.dumps(policy, indent=2) + "\n", encoding="utf-8"
    )
    terms = load_input(terms_fixture / "input.json")
    terms["instrumentTerms"][0]["tickSize"] = "0.02"
    (terms_fixture / "input.json").write_text(json.dumps(terms, indent=2) + "\n", encoding="utf-8")
    assert run_fixture(policy_fixture).manifest_hash != baseline
    assert run_fixture(terms_fixture).manifest_hash != baseline


def test_negative_invariant_mutations_are_detected() -> None:
    executions, episodes, allocations, matches = _internal_result(
        FIXTURE_ROOT / "scenario-01-simple-long"
    )
    assert invariant_errors(executions, episodes, allocations, matches) == []

    quantity_allocations = deepcopy(allocations)
    quantity_allocations[0].quantity += Decimal(1)
    assert any(
        error.startswith("EXECUTION_QUANTITY_CONSERVATION")
        for error in invariant_errors(executions, episodes, quantity_allocations, matches)
    )

    fee_allocations = deepcopy(allocations)
    fee_allocations[0].fee += Decimal("0.01")
    assert any(
        error.startswith("FEE_CONSERVATION")
        for error in invariant_errors(executions, episodes, fee_allocations, matches)
    )

    pnl_allocations = deepcopy(allocations)
    pnl_allocations[1].reported_pnl += Decimal("0.01")
    assert any(
        error.startswith("REPORTED_PNL_CONSERVATION")
        for error in invariant_errors(executions, episodes, pnl_allocations, matches)
    )

    volume_episodes = deepcopy(episodes)
    volume_episodes[0].opening_volume += Decimal(1)
    assert any(
        error.startswith("COMPLETE_VOLUME_CONSERVATION")
        for error in invariant_errors(executions, volume_episodes, allocations, matches)
    )


def test_negative_graph_and_quality_mutations_are_detected() -> None:
    executions, episodes, allocations, matches = _internal_result(
        FIXTURE_ROOT / "scenario-01-simple-long"
    )
    duplicate_matches = matches + [deepcopy(matches[0])]
    assert "SUMMARY_ASSIGNED_TO_MULTIPLE_ELIGIBLE_EPISODES" in invariant_errors(
        executions, episodes, allocations, duplicate_matches
    )

    ambiguous_matches = deepcopy(matches)
    ambiguous_matches[0]["status"] = "AMBIGUOUS"
    assert any(
        error.startswith("AMBIGUOUS_HEURISTIC_SELECTION")
        for error in invariant_errors(executions, episodes, allocations, ambiguous_matches)
    )

    censored_executions, censored, censored_allocations, censored_matches = _internal_result(
        FIXTURE_ROOT / "scenario-10-right-censored"
    )
    censored[0].eligible = True
    assert any(
        error.startswith("INELIGIBLE_RECONSTRUCTION_PROMOTION")
        for error in invariant_errors(
            censored_executions, censored, censored_allocations, censored_matches
        )
    )

    unvalued_executions, unvalued, unvalued_allocations, unvalued_matches = _internal_result(
        FIXTURE_ROOT / "scenario-20-different-fee-asset"
    )
    unvalued[0].capabilities.add("CLOSED_OUTCOME")
    assert any(
        error.startswith("MISSING_CAPABILITY_AS_ZERO")
        for error in invariant_errors(
            unvalued_executions,
            unvalued,
            unvalued_allocations,
            unvalued_matches,
        )
    )

    missing_provenance = deepcopy(allocations)
    missing_provenance[0].provenance.clear()
    assert any(
        error.startswith("MISSING_PROVENANCE")
        for error in invariant_errors(executions, episodes, missing_provenance, matches)
    )


def test_ambiguous_duplicate_summaries_are_not_resolved_by_a_heuristic(tmp_path: Path) -> None:
    fixture = tmp_path / "ambiguous"
    shutil.copytree(FIXTURE_ROOT / "scenario-01-simple-long", fixture)
    position_path = fixture / "position_history.csv"
    lines = position_path.read_text(encoding="utf-8").splitlines()
    position_path.write_text("\n".join([*lines, lines[1]]) + "\n", encoding="utf-8")
    actual = run_fixture(fixture).actual
    assert actual["episodes"][0]["reconciliationStatus"] == "AMBIGUOUS"
    assert actual["episodes"][0]["eligible"] is False
    assert all(match["episodeKey"] is None for match in actual["summaryMatches"])


def test_reversal_duplicate_quantity_assignment_is_detected() -> None:
    executions, episodes, allocations, matches = _internal_result(
        FIXTURE_ROOT / "scenario-07-long-to-short-reversal"
    )
    reversal = next(item for item in allocations if item.action == "REVERSAL_CLOSE")
    allocations.append(deepcopy(reversal))
    errors = invariant_errors(executions, episodes, allocations, matches)
    assert any(error.startswith("REVERSAL_DISJOINT_SPLIT") for error in errors)


def test_non_reversal_duplicate_execution_assignment_is_detected() -> None:
    executions, episodes, allocations, matches = _internal_result(
        FIXTURE_ROOT / "scenario-01-simple-long"
    )
    duplicate = deepcopy(allocations[0])
    allocations[0].quantity = Decimal(1)
    duplicate.quantity = Decimal(1)
    allocations.append(duplicate)
    errors = invariant_errors(executions, episodes, allocations, matches)
    assert any(error.startswith("DUPLICATE_EXECUTION_ASSIGNMENT") for error in errors)
