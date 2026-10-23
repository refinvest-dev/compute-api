"""Independent executable expectations from context TRADING_ANALYTICS.md §13."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from random import Random

import pytest

from refinvest.trading_review.analytics import (
    AnalysisConfig,
    AnalysisPeriod,
    calculate_behavior_metrics,
)
from refinvest.trading_review.analytics.engine import (
    classify_episode,
    classify_outcome,
    distribution,
)
from refinvest.trading_review.analytics.model import (
    ExposureValues,
    HoldingValues,
    MetricStatus,
    Outcome,
    ReentryValues,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.hashing import canonical_json_bytes
from refinvest.trading_review.model import (
    AllocationAction,
    Capability,
    CapabilitySnapshot,
    Direction,
    ExecutionRecord,
    FuturesPositionEpisode,
    InstrumentDescriptor,
    MoneyAmount,
    PositionAllocation,
    ReconciliationStatus,
    ReconstructionStatus,
    Side,
    SourceArtifactRole,
    SourceRowProvenance,
)


def instant(clock: str) -> datetime:
    hour, minute, *seconds = clock.split(":")
    second = int(seconds[0].split(".")[0]) if seconds else 0
    microsecond = (
        int(seconds[0].split(".")[1].ljust(6, "0")) if seconds and "." in seconds[0] else 0
    )
    return datetime(2026, 1, 1, int(hour), int(minute), second, microsecond, tzinfo=UTC)


def number(value: str | int | Decimal) -> ScaledDecimal:
    return ScaledDecimal.parse(str(value))


def rational(value) -> Fraction:
    return Fraction(int(value.numerator), int(value.denominator))


def terms(symbol: str = "A", currency: str = "USDx") -> InstrumentDescriptor:
    return InstrumentDescriptor(
        symbol, symbol, currency, number(1), number(1), number(1), 8, "SYNTHETIC_V1"
    )


INSTRUMENTS = (terms(), terms("B"), terms("C", "USDy"))


def episode(
    identity: str,
    *,
    opened: str = "09:30",
    closed: str = "10:00",
    outcome: str = "LOSS",
    initial: str = "100",
    instrument: str = "A",
    direction: Direction = Direction.LONG,
    opening: AllocationAction = AllocationAction.OPEN,
    closing: AllocationAction = AllocationAction.CLOSE,
    fee: str = "1",
    missing_fees: bool = False,
    missing_time: bool = False,
    missing_exposure: bool = False,
    reconstruction: ReconstructionStatus = ReconstructionStatus.COMPLETE,
    reconciliation: ReconciliationStatus = ReconciliationStatus.EXACT,
) -> tuple[FuturesPositionEpisode, tuple[ExecutionRecord, ExecutionRecord]]:
    open_at, close_at = instant(opened), instant(closed)
    currency = next(
        item.settlement_asset for item in INSTRUMENTS if item.instrument_id == instrument
    )

    def provenance(row: int) -> tuple[SourceRowProvenance, ...]:
        return (
            SourceRowProvenance(
                "source",
                SourceArtifactRole.TRADE_HISTORY,
                "source-hash",
                row,
                f"row-{identity}-{row}",
            ),
        )

    records = tuple(
        ExecutionRecord(
            f"{identity}-{suffix}",
            f"hash-{identity}-{suffix}",
            f"key-{identity}-{suffix}",
            instrument,
            instrument,
            at,
            Side.BUY if suffix == "open" else Side.SELL,
            number(initial if suffix == "open" else 1),
            number(1),
            number(1),
            suffix == "open",
            False,
            "SYNTHETIC",
            provenance(index),
            str(index),
            index,
        )
        for index, suffix, at in ((1, "open", open_at), (2, "close", close_at))
    )
    fee_value = Decimal(fee)
    target_net = {"LOSS": Decimal("-10"), "BREAKEVEN": Decimal(0), "WIN": Decimal("10")}.get(
        outcome
    )
    gross_value = target_net + fee_value if target_net is not None else None
    net_value = None if missing_fees else target_net
    allocations = tuple(
        PositionAllocation(
            record.record_id,
            record.source_execution_key,
            identity,
            action,
            number(1),
            MoneyAmount(number(fee if index == 1 and not missing_fees else 0), currency),
            MoneyAmount(
                number(gross_value if index == 1 and gross_value is not None else 0), currency
            ),
            None,
            (record.record_id,),
        )
        for index, (record, action) in enumerate(zip(records, (opening, closing), strict=True))
    )
    caps = [
        Capability.CLOSED_OUTCOME,
        Capability.FEES,
        Capability.STABLE_EPISODE_TIME,
        Capability.INITIAL_EXPOSURE,
    ]
    if missing_fees:
        caps.remove(Capability.FEES)
        caps.remove(Capability.CLOSED_OUTCOME)
    if missing_time:
        caps.remove(Capability.STABLE_EPISODE_TIME)
    if missing_exposure:
        caps.remove(Capability.INITIAL_EXPOSURE)
    snapshot = CapabilitySnapshot(
        tuple(caps), True, net_value is not None, True, not missing_time, True, False, ()
    )
    result = FuturesPositionEpisode(
        identity,
        instrument,
        instrument,
        direction,
        open_at,
        close_at,
        None if missing_exposure else number(1),
        number(1),
        number(1),
        number(1),
        None,
        None,
        MoneyAmount(number(gross_value), currency) if gross_value is not None else None,
        () if missing_fees else (MoneyAmount(number(fee), currency),),
        MoneyAmount(number(net_value), currency) if net_value is not None else None,
        reconstruction,
        reconciliation,
        allocations,
        snapshot,
        reconstruction is ReconstructionStatus.COMPLETE
        and reconciliation is ReconciliationStatus.EXACT,
        (),
        tuple(record.record_id for record in records),
    )
    return result, records  # type: ignore[return-value]


def run(
    *items: tuple[FuturesPositionEpisode, tuple[ExecutionRecord, ExecutionRecord]],
    end: str = "12:00",
):
    config = AnalysisConfig(AnalysisPeriod(instant("09:00"), instant(end)), "UTC", "pinned-tzdb")
    return calculate_behavior_metrics(
        episodes=tuple(item[0] for item in items),
        canonical_records=tuple(record for item in items for record in item[1]),
        instruments=INSTRUMENTS,
        analysis_config=config,
    )


@pytest.mark.parametrize(
    ("scenario", "entry_open", "entry_instrument", "entry_action", "expected_entry", "role"),
    [
        (1, "10:29:59", "A", AllocationAction.OPEN, True, "SELECTED_EARLIEST"),
        (2, "10:30", "A", AllocationAction.OPEN, True, "SELECTED_EARLIEST"),
        (3, "10:30:00.000001", "A", AllocationAction.OPEN, False, "AFTER_WINDOW"),
        (4, "10:00", "A", AllocationAction.OPEN, False, "SAME_TIMESTAMP"),
        (5, "10:00", "A", AllocationAction.REVERSAL_OPEN, False, "REVERSAL_SEPARATE"),
        (6, "10:10", "A", AllocationAction.OPEN, True, "SELECTED_EARLIEST"),
        (7, "10:10", "B", AllocationAction.OPEN, True, "SELECTED_EARLIEST"),
    ],
)
def test_reentry_boundary_scenarios_1_to_7(
    scenario, entry_open, entry_instrument, entry_action, expected_entry, role
):
    loss = episode(
        f"L{scenario:02d}",
        closing=AllocationAction.REVERSAL_CLOSE if scenario == 5 else AllocationAction.CLOSE,
    )
    entry = episode(
        f"E{scenario:02d}",
        opened=entry_open,
        closed="11:00",
        outcome="WIN",
        instrument=entry_instrument,
        opening=entry_action,
    )
    metric = run(loss, entry)[0]
    assert metric.status is MetricStatus.AVAILABLE
    assert isinstance(metric.values, ReentryValues)
    assert metric.values.eligible_loss_count == 1
    assert metric.values.loss_with_entry_count == int(expected_entry)
    assert rational(metric.values.entry_within_window_rate) == int(expected_entry)
    assert metric.relations[0].candidates[0].role == role
    assert metric.relations[0].related_entry_ids == (
        (entry[0].episode_id,) if expected_entry else ()
    )
    assert {ref.episode_id for ref in metric.evidence_references} == {
        loss[0].episode_id,
        entry[0].episode_id,
    }


def test_reentry_scenario_8_simultaneous_cross_instrument():
    metric = run(
        episode("L08"),
        episode("E08A", opened="10:10", closed="11:00", outcome="WIN"),
        episode("E08B", opened="10:10", closed="11:00", outcome="WIN", instrument="B"),
    )[0]
    assert metric.status is MetricStatus.AVAILABLE
    assert isinstance(metric.values, ReentryValues)
    assert metric.relations[0].related_entry_ids == ("E08A", "E08B")
    assert (
        metric.values.loss_with_entry_count,
        metric.values.related_entry_count,
        metric.values.same_symbol_count,
        metric.values.different_symbol_count,
    ) == (1, 2, 1, 1)


def test_reentry_scenario_9_later_entry_is_audit_evidence():
    metric = run(
        episode("L09"),
        episode("E09A", opened="10:05", closed="11:00", outcome="WIN"),
        episode("E09B", opened="10:20", closed="11:00", outcome="WIN"),
    )[0]
    assert metric.relations[0].related_entry_ids == ("E09A",)
    assert [(item.entry_id, item.role) for item in metric.relations[0].candidates] == [
        ("E09A", "SELECTED_EARLIEST"),
        ("E09B", "LATER_NOT_LINKED"),
    ]
    assert metric.relations[0].elapsed_microseconds == 300_000_000
    assert "E09B" in {item.episode_id for item in metric.excluded_membership}


def test_reentry_scenario_10_right_window_censored():
    censored = run(episode("L10"), end="10:20")[0]
    assert censored.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert censored.excluded_reason_counts == (("WINDOW_RIGHT_CENSORED", 1),)
    assert censored.values.entry_within_window_rate is None


def test_reentry_scenario_11_missing_fee_capability():
    missing = run(
        episode("L11", missing_fees=True),
        episode("E11", opened="10:10", closed="11:00", outcome="WIN"),
    )[0]
    assert missing.status is MetricStatus.MISSING_CAPABILITY
    assert {"MISSING_FEES", "OUTCOME_UNAVAILABLE"} <= dict(missing.excluded_reason_counts).keys()
    assert missing.values.entry_within_window_rate is None


def test_reentry_scenario_12_breakeven_not_loss():
    breakeven = run(
        episode("B12", outcome="BREAKEVEN"),
        episode("E12", opened="10:10", closed="12:30", outcome="WIN"),
    )[0]
    assert breakeven.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert breakeven.excluded_reason_counts == (("OUTCOME_NOT_LOSS", 1),)


def prior(prefix: str, count: int = 3, exposure: str = "100"):
    return tuple(
        episode(
            f"{prefix}{letter}",
            opened=f"09:0{index}",
            closed=f"09:1{index}",
            outcome="WIN",
            initial=exposure,
        )
        for index, letter in enumerate("ABC"[:count])
    )


@pytest.mark.parametrize(
    ("scenario", "entry", "expected_ratio", "direction"),
    [
        (13, "150", Fraction(3, 2), "increased_count"),
        (14, "60", Fraction(3, 5), "decreased_count"),
        (15, "100", Fraction(1), "equal_count"),
    ],
)
def test_exposure_scenarios_13_to_15(scenario, entry, expected_ratio, direction):
    metric = run(
        *prior(f"H{scenario}"),
        episode(f"L{scenario}", initial="100"),
        episode(f"E{scenario}", opened="10:10", closed="11:00", outcome="WIN", initial=entry),
    )[1]
    assert metric.status is MetricStatus.AVAILABLE
    assert isinstance(metric.values, ExposureValues)
    value = metric.values.median_next_entry_vs_loss_episode_ratio
    assert rational(value) == expected_ratio
    assert getattr(metric.values, direction) == 1
    assert metric.values.pairs[0].baseline_episode_ids == tuple(
        f"H{scenario}{letter}" for letter in "ABC"
    )
    assert len(metric.evidence_references) == 5


@pytest.mark.parametrize(
    ("scenario", "baseline_count", "exposure", "reason"),
    [
        (16, 3, "0", "ZERO_DENOMINATOR"),
        (17, 2, "100", "INSUFFICIENT_PRIOR_BASELINE"),
    ],
)
def test_exposure_scenarios_16_and_17_baseline_guards(scenario, baseline_count, exposure, reason):
    metric = run(
        *prior(f"P{scenario}", count=baseline_count, exposure=exposure),
        episode(f"L{scenario}"),
        episode(f"E{scenario}", opened="10:10", closed="11:00", outcome="WIN", initial="120"),
    )[1]
    assert metric.status is MetricStatus.AVAILABLE
    assert isinstance(metric.values, ExposureValues)
    assert metric.values.pairs[0].baseline_reason == reason
    assert len(metric.values.pairs[0].baseline_episode_ids) == baseline_count
    assert metric.values.pairs[0].baseline_ratio is None
    assert rational(metric.values.median_next_entry_vs_loss_episode_ratio) == Fraction(6, 5)


def test_exposure_scenario_18_simultaneous_pairs():
    multiple = run(
        *prior("H18"),
        episode("L18"),
        episode("E18A", opened="10:10", closed="11:00", outcome="WIN", initial="150"),
        episode(
            "E18B", opened="10:10", closed="11:00", outcome="WIN", initial="50", instrument="B"
        ),
    )[1]
    assert multiple.status is MetricStatus.AVAILABLE
    assert isinstance(multiple.values, ExposureValues)
    assert multiple.values.pair_count == 2
    assert tuple(item.episode_id for item in multiple.subject_membership) == ("L18",)
    assert multiple.population_count == multiple.eligible_count + multiple.excluded_count
    assert (multiple.values.increased_count, multiple.values.decreased_count) == (1, 1)
    assert rational(multiple.values.median_next_entry_vs_loss_episode_ratio) == 1


def test_exposure_scenario_19_currency_mismatch():
    currency = run(
        episode("L19"),
        episode("E19", opened="10:10", closed="11:00", outcome="WIN", instrument="C"),
    )[1]
    assert currency.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert currency.values.pair_count == 0
    assert ("SETTLEMENT_CURRENCY_MISMATCH", 1) in currency.excluded_reason_counts


def test_exposure_scenario_20_outlier_is_retained():
    items = [
        episode(f"L20{letter}", initial="100", closed=close)
        for letter, close in zip("ABC", ("10:00", "10:40", "11:20"), strict=True)
    ]
    items += [
        episode(f"E20{letter}", opened=opened, closed=closed, outcome="WIN", initial=initial)
        for letter, opened, closed, initial in zip(
            "ABC",
            ("10:05", "10:45", "11:25"),
            ("10:30", "11:10", "11:55"),
            ("100", "100", "10000"),
            strict=True,
        )
    ]
    metric = run(*items)[1]
    assert metric.status is MetricStatus.AVAILABLE
    assert metric.values.pair_count == 3
    assert rational(metric.values.median_next_entry_vs_loss_episode_ratio) == 1
    assert rational(metric.values.ratio_distribution.maximum) == 100


def holding_items(scenario: int, wins: tuple[int, ...], losses: tuple[int, ...]):
    close = instant("11:00")
    items = []
    for outcome, durations in (("WIN", wins), ("LOSS", losses)):
        for index, duration in enumerate(durations):
            opened = close - timedelta(minutes=duration)
            identity = f"{'W' if outcome == 'WIN' else 'L'}{scenario}{chr(65 + index)}"
            if scenario == 26 and index == 0 and outcome == "LOSS":
                items.append(
                    episode(
                        identity,
                        opened="10:30",
                        closed="10:40",
                        outcome=outcome,
                        closing=AllocationAction.REVERSAL_CLOSE,
                    )
                )
                continue
            if scenario == 26 and index == 0 and outcome == "WIN":
                items.append(
                    episode(
                        identity,
                        opened="10:40",
                        closed="11:00",
                        outcome=outcome,
                        opening=AllocationAction.REVERSAL_OPEN,
                    )
                )
                continue
            item = episode(
                identity, opened=opened.strftime("%H:%M"), closed="11:00", outcome=outcome
            )
            if opened.date() != close.date():
                item = (
                    replace(item[0], opened_at=opened),
                    (replace(item[1][0], occurred_at=opened), item[1][1]),
                )
            items.append(item)
    return tuple(items)


@pytest.mark.parametrize(
    ("scenario", "wins", "losses", "status", "difference"),
    [
        (21, (10, 20), (30, 50), MetricStatus.AVAILABLE, 25),
        (22, (10,), (30, 50), MetricStatus.INSUFFICIENT_SAMPLE, None),
        (23, (10, 20), (30,), MetricStatus.INSUFFICIENT_SAMPLE, None),
        (25, (0, 0), (5, 5), MetricStatus.AVAILABLE, 5),
        (26, (20, 20), (10, 10), MetricStatus.AVAILABLE, -10),
        (27, (10, 10, 1000), (20, 20, 20), MetricStatus.AVAILABLE, 10),
        (28, (10, 20), (30, 50), MetricStatus.AVAILABLE, 25),
    ],
)
def test_holding_scenarios_21_to_28(scenario, wins, losses, status, difference):
    metric = run(*holding_items(scenario, wins, losses))[2]
    assert metric.status is status
    assert isinstance(metric.values, HoldingValues)
    assert (metric.values.win_count, metric.values.loss_count) == (len(wins), len(losses))
    assert len(metric.subject_membership) == len(wins)
    assert len(metric.comparison_membership) == len(losses)
    assert len(metric.evidence_references) == len(wins) + len(losses)
    if difference is None:
        assert metric.values.median_difference_microseconds is None
    else:
        value = metric.values.median_difference_microseconds
        assert rational(value) == difference * 60_000_000
    if scenario == 25:
        assert metric.values.median_ratio is None
        assert metric.values.median_ratio_reason == "ZERO_DENOMINATOR"
    if scenario == 27:
        assert rational(metric.values.win_distribution.mean) == 340 * 60_000_000
    if scenario == 26:
        source = holding_items(scenario, wins, losses)
        assert source[0][0].allocations[0].action is AllocationAction.REVERSAL_OPEN
        assert source[2][0].allocations[-1].action is AllocationAction.REVERSAL_CLOSE
        assert source[0][0].opened_at == source[2][0].closed_at


def test_holding_scenario_24_breakeven_only():
    metric = run(
        episode("B24A", opened="10:50", closed="11:00", outcome="BREAKEVEN"),
        episode("B24B", opened="10:40", closed="11:00", outcome="BREAKEVEN"),
    )[2]
    assert metric.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert metric.values.breakeven_count == 2
    assert metric.excluded_reason_counts == (("OUTCOME_BREAKEVEN", 2),)


def test_capability_quality_and_empty_period_are_distinct():
    missing_time = run(episode("Ltime", missing_time=True))[0]
    missing_exposure = run(
        episode("Lexp"),
        episode("Eexp", opened="10:10", closed="11:00", outcome="WIN", missing_exposure=True),
    )[1]
    excluded = run(episode("Lquality", reconstruction=ReconstructionStatus.LEFT_CENSORED))[0]
    empty = run()[0]
    assert missing_time.status is MetricStatus.MISSING_CAPABILITY
    assert missing_exposure.status is MetricStatus.MISSING_CAPABILITY
    assert excluded.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert excluded.excluded_reason_counts == (("RECONSTRUCTION_NOT_COMPLETE", 1),)
    assert empty.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert empty.population_count == empty.eligible_count == empty.excluded_count == 0


def test_primary_zero_denominator_and_unavailable_outcome_are_not_zero():
    zero = run(
        episode("Lzero", initial="0"),
        episode("Ezero", opened="10:10", closed="11:00", outcome="WIN"),
    )[1]
    assert zero.status is MetricStatus.INSUFFICIENT_SAMPLE
    assert zero.values.pair_count == 0
    assert zero.values.median_next_entry_vs_loss_episode_ratio is None
    assert ("ZERO_DENOMINATOR", 1) in zero.excluded_reason_counts
    source, records = episode("Lnull")
    unavailable = run((replace(source, net_trading_pnl=None), records))[0]
    assert unavailable.status is MetricStatus.MISSING_CAPABILITY
    assert unavailable.values.entry_within_window_rate is None
    assert unavailable.excluded_reason_counts == (("OUTCOME_UNAVAILABLE", 1),)
    assert source.gross_realized_pnl is not None


def test_entry_time_capability_blocks_partial_reentry_rate():
    metric = run(
        episode("Lcandidate"),
        episode("Ecandidate", opened="10:10", closed="11:00", outcome="WIN", missing_time=True),
    )[0]
    assert metric.status is MetricStatus.MISSING_CAPABILITY
    assert metric.values.entry_within_window_rate is None
    assert ("MISSING_STABLE_EPISODE_TIME", 1) in metric.excluded_reason_counts
    assert metric.finding_eligibility == "INELIGIBLE_METRIC_STATUS"


def test_unrelated_win_time_gap_does_not_gate_reentry():
    metric = run(
        episode("Lknown"),
        episode("Wunrelated", opened="09:40", closed="11:00", outcome="WIN", missing_time=True),
    )[0]
    assert metric.status is MetricStatus.AVAILABLE
    assert metric.values.loss_with_entry_count == 0
    assert rational(metric.values.entry_within_window_rate) == 0
    assert metric.excluded_reason_counts == (("OUTCOME_NOT_LOSS", 1),)


def test_baseline_uses_only_same_instrument_direction_strictly_before_event():
    history = prior("Pstrict")
    same_close = episode("Pboundary", opened="09:20", closed="10:00", outcome="WIN")
    wrong_direction = episode(
        "Pshort", opened="09:20", closed="09:30", outcome="WIN", direction=Direction.SHORT
    )
    wrong_instrument = episode(
        "Pother", opened="09:20", closed="09:30", outcome="WIN", instrument="B"
    )
    metric = run(
        *history,
        same_close,
        wrong_direction,
        wrong_instrument,
        episode("Lstrict"),
        episode("Estrict", opened="10:10", closed="11:00", outcome="WIN"),
    )[1]
    assert metric.status is MetricStatus.AVAILABLE
    assert metric.values.pairs[0].baseline_episode_ids == ("PstrictA", "PstrictB", "PstrictC")
    assert metric.finding_eligibility == "INELIGIBLE_SAMPLE"


def test_missing_baseline_capability_is_recorded_without_lowering_primary_status():
    baseline = episode(
        "Pmissing", opened="09:20", closed="09:30", outcome="WIN", missing_exposure=True
    )
    metric = run(
        *prior("Pok"),
        baseline,
        episode("Lbaseline"),
        episode("Ebaseline", opened="10:10", closed="11:00", outcome="WIN"),
    )[1]
    assert metric.status is MetricStatus.AVAILABLE
    assert metric.values.pairs[0].baseline_episode_ids == ("PokA", "PokB", "PokC")
    assert ("MISSING_INITIAL_EXPOSURE", 1) in metric.excluded_reason_counts
    assert "Pmissing" in {item.episode_id for item in metric.excluded_membership}


def test_outcome_exact_sign_rebate_and_no_gross_fallback():
    loss, loss_records = episode("loss", fee="-2")
    zero, _ = episode("zero", outcome="BREAKEVEN")
    win, _ = episode("win", outcome="WIN")
    unavailable, _ = episode("unavailable", missing_fees=True)
    assert tuple(classify_outcome(item) for item in (loss, zero, win, unavailable)) == (
        Outcome.LOSS,
        Outcome.BREAKEVEN,
        Outcome.WIN,
        Outcome.UNAVAILABLE,
    )
    assert loss.gross_realized_pnl.amount.value == Decimal("-12")
    assert loss.net_trading_pnl.amount.value == Decimal("-10")
    classification = classify_episode(loss, loss_records)
    assert classification.outcome is Outcome.LOSS
    assert classification.allocated_fees[1][1] == Decimal("-2")
    assert classification.allocated_reported_pnl[1][1] == Decimal("-12")
    assert {row[2] for row in classification.evidence.source_rows} == {
        "row-loss-1",
        "row-loss-2",
    }


def test_fractional_even_median_and_decimal_context_independence():
    assert distribution((1, 2)).median.numerator == "3"
    assert distribution((1, 2)).median.denominator == "2"
    items = (
        episode("Lprec", initial="1000000000000000000000000000000"),
        episode(
            "Eprec",
            opened="10:10",
            closed="11:00",
            outcome="WIN",
            initial="1500000000000000000000000000000",
        ),
    )
    with localcontext() as context:
        context.prec = 3
        low = run(*items)
    with localcontext() as context:
        context.prec = 100
        high = run(*items)
    assert low == high
    assert low[1].values.median_next_entry_vs_loss_episode_ratio.numerator == "3"
    assert low[1].values.median_next_entry_vs_loss_episode_ratio.denominator == "2"


def test_input_order_repetition_and_evidence_order_are_deterministic():
    items = (
        episode("Lorder"),
        episode("EorderB", opened="10:10", closed="11:00", outcome="WIN", instrument="B"),
        episode("EorderA", opened="10:10", closed="11:00", outcome="WIN"),
    )
    expected = run(*items)
    for seed in range(10):
        shuffled = list(items)
        Random(seed).shuffle(shuffled)
        assert run(*shuffled) == expected
        assert canonical_json_bytes(run(*shuffled)) == canonical_json_bytes(expected)
    assert expected[0].relations[0].related_entry_ids == ("EorderA", "EorderB")
    assert tuple(ref.episode_id for ref in expected[0].evidence_references) == (
        "EorderA",
        "EorderB",
        "Lorder",
    )
    assert all(ref.source_rows for ref in expected[0].evidence_references)


@pytest.mark.parametrize(
    "change",
    [
        {"reentry_window": "PT31M"},
        {"funding_treatment": "INCLUDED"},
        {"review_timezone": ""},
    ],
)
def test_invalid_analysis_config_fails_validation(change):
    with pytest.raises(ValueError):
        replace(
            AnalysisConfig(
                AnalysisPeriod(instant("09:00"), instant("12:00")), "UTC", "pinned-tzdb"
            ),
            **change,
        )


def test_empty_analysis_period_fails_validation():
    with pytest.raises(ValueError):
        AnalysisPeriod(instant("09:00"), instant("09:00"))
