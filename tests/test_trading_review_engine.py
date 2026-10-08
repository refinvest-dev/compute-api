"""Production-engine golden scenarios derived from Context ADR-058/059."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from refinvest.trading_review import FailureCode, TradingReviewError, normalize_and_reconstruct
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.model import (
    AllocationAction,
    FeeValuation,
    ReconciliationStatus,
    ReconstructionStatus,
)
from tests.trading_review_support import position_row, request_for, simple_long_request, trade_row


def test_trading_review_simple_long_golden_result() -> None:
    result = normalize_and_reconstruct(simple_long_request())
    episode = result.episodes[0]
    assert episode.direction.value == "LONG"
    assert episode.reconstruction_status is ReconstructionStatus.COMPLETE
    assert episode.reconciliation_status is ReconciliationStatus.EXACT
    assert episode.eligible is True
    assert [item.action for item in episode.allocations] == [
        AllocationAction.OPEN,
        AllocationAction.CLOSE,
    ]
    assert episode.average_entry_price is not None
    assert episode.average_entry_price.value == Decimal("10")
    assert episode.average_close_price is not None
    assert episode.average_close_price.value == Decimal("11")
    assert episode.gross_realized_pnl is not None
    assert episode.gross_realized_pnl.amount.source_lexeme == "2.00"
    assert result.quality_report.eligible_count == 1
    assert result.reconciliation_manifest.quantity_conservation_passed
    assert result.reconciliation_manifest.fee_conservation_passed
    assert result.reconciliation_manifest.pnl_conservation_passed


def test_trading_review_simple_short_and_partial_entry_exit() -> None:
    short_trades = [
        trade_row("201", "SELL", "3", "20", time="2026-01-02 10:00:01"),
        trade_row("202", "BUY", "3", "18", "6", time="2026-01-02 10:06:01"),
    ]
    short_position = [
        position_row("Short", "20", "18", "3", "3", "6", closed="2026-01-02 10:06:01")
    ]
    short = normalize_and_reconstruct(request_for(short_trades, short_position)).episodes[0]
    assert short.direction.value == "SHORT"
    assert short.eligible

    partial_trades = [
        trade_row("301", "BUY", "1", "10", time="2026-01-02 10:00:01"),
        trade_row("302", "BUY", "2", "13", time="2026-01-02 10:01:01"),
        trade_row("303", "SELL", "1", "14", "2", time="2026-01-02 10:02:01"),
        trade_row("304", "SELL", "2", "14", "4", time="2026-01-02 10:05:01"),
    ]
    partial_position = [position_row("Long", "12", "14", "3", "3", "6")]
    partial = normalize_and_reconstruct(request_for(partial_trades, partial_position)).episodes[0]
    assert [item.action.value for item in partial.allocations] == [
        "OPEN",
        "INCREASE",
        "REDUCE",
        "CLOSE",
    ]
    assert partial.average_entry_price is not None
    assert partial.average_entry_price.value == Decimal("12")
    assert partial.average_close_price is not None
    assert partial.average_close_price.value == Decimal("14")


@pytest.mark.parametrize(
    ("opening_side", "reversal_side", "closing_side", "new_direction"),
    [("BUY", "SELL", "BUY", "Short"), ("SELL", "BUY", "SELL", "Long")],
)
def test_trading_review_reversal_conserves_quantity_fee_and_pnl(
    opening_side: str,
    reversal_side: str,
    closing_side: str,
    new_direction: str,
) -> None:
    opening_quantity = "3" if opening_side == "BUY" else "4"
    reversal_quantity = "5" if opening_side == "BUY" else "6"
    reversal_pnl = "-3" if opening_side == "BUY" else "-4"
    reversal_fee = "0.05USDT" if opening_side == "BUY" else "0.06USDT"
    trades = [
        trade_row("701", opening_side, opening_quantity, "10"),
        trade_row(
            "702",
            reversal_side,
            reversal_quantity,
            "9" if opening_side == "BUY" else "11",
            reversal_pnl,
            reversal_fee,
            time="2026-01-02 10:05:01",
        ),
        trade_row(
            "703",
            closing_side,
            "2",
            "8" if opening_side == "BUY" else "12",
            "2",
            time="2026-01-02 10:10:01",
        ),
    ]
    positions = [
        position_row(
            "Long" if opening_side == "BUY" else "Short",
            "10",
            "9" if opening_side == "BUY" else "11",
            opening_quantity,
            opening_quantity,
            reversal_pnl,
        ),
        position_row(
            new_direction,
            "9" if opening_side == "BUY" else "11",
            "8" if opening_side == "BUY" else "12",
            "2",
            "2",
            "2",
            opened="2026-01-02 10:05:01",
            closed="2026-01-02 10:10:01",
        ),
    ]
    result = normalize_and_reconstruct(request_for(trades, positions))
    reversal = [
        allocation
        for episode in result.episodes
        for allocation in episode.allocations
        if allocation.reversal_group_id is not None
    ]
    assert len(result.canonical_records) == 11
    assert [item.action for item in reversal] == [
        AllocationAction.REVERSAL_CLOSE,
        AllocationAction.REVERSAL_OPEN,
    ]
    assert sum((item.allocated_quantity.value for item in reversal), Decimal(0)) == Decimal(
        reversal_quantity
    )
    assert sum((item.allocated_fee.amount.value for item in reversal), Decimal(0)) == Decimal(
        reversal_fee.removesuffix("USDT")
    )
    assert reversal[1].allocated_reported_pnl.amount.value == 0
    assert reversal[0].reversal_group_id == reversal[1].reversal_group_id
    assert all(episode.eligible for episode in result.episodes)


def test_trading_review_numeric_order_and_same_order_fills() -> None:
    trades = [
        trade_row("302", "SELL", "1", "11", "1", order_id="900", time="2026-01-02 10:00:01"),
        trade_row("301", "BUY", "2", "10", order_id="900", time="2026-01-02 10:00:01"),
    ]
    result = normalize_and_reconstruct(request_for(trades, []))
    assert [item.action.value for item in result.episodes[0].allocations] == ["OPEN", "REDUCE"]
    executions = [record for record in result.canonical_records if hasattr(record, "side")]
    assert len(executions) == 2


def test_trading_review_duplicate_and_identity_conflict() -> None:
    opening = trade_row("1701", "BUY", "1", "10")
    close = trade_row("1702", "SELL", "1", "11", "1", time="2026-01-02 10:05:01")
    result = normalize_and_reconstruct(
        request_for(
            [opening, opening.copy(), close],
            [position_row("Long", "10", "11", "1", "1", "1")],
        )
    )
    assert len(result.canonical_records[0].provenance) == 2
    assert result.quality_report.duplicate_count == 1
    conflict = opening.copy()
    conflict[5] = "2"
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request_for([opening, conflict], []))
    assert raised.value.code is FailureCode.RECORD_CONFLICT


def test_trading_review_numeric_equivalent_trade_id_conflicts() -> None:
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(
            request_for(
                [
                    trade_row("01", "BUY", "1", "10"),
                    trade_row("1", "BUY", "1", "10", time="2026-01-02 10:01:01"),
                ],
                [],
            )
        )
    assert raised.value.code is FailureCode.RECORD_CONFLICT


def test_trading_review_censoring_remains_ineligible_without_inference() -> None:
    right = normalize_and_reconstruct(request_for([trade_row("1", "BUY", "2", "10")], [])).episodes[
        0
    ]
    assert right.reconstruction_status is ReconstructionStatus.RIGHT_CENSORED
    assert right.closed_at is None
    assert not right.eligible

    left_result = normalize_and_reconstruct(
        request_for(
            [trade_row("1", "BUY", "2", "9", "2", time="2026-01-02 10:00:01")],
            [
                position_row(
                    "Short",
                    "10",
                    "9",
                    "2",
                    "2",
                    "2",
                    opened="2026-01-02 09:50:00",
                    closed="2026-01-02 10:00:01",
                )
            ],
        )
    )
    shell = next(
        item
        for item in left_result.episodes
        if item.reconstruction_status is ReconstructionStatus.LEFT_CENSORED
    )
    assert shell.opened_at is None
    assert shell.initial_quantity is None
    assert not shell.eligible


def test_trading_review_missing_execution_summary_and_position_only_are_explicit() -> None:
    incomplete = normalize_and_reconstruct(
        request_for(
            [
                trade_row("1", "BUY", "3", "10"),
                trade_row("2", "SELL", "2", "11", "2", time="2026-01-02 10:05:01"),
            ],
            [position_row("Long", "10", "11", "3", "3", "3")],
        )
    )
    assert any("EPISODE_RIGHT_CENSORED" in item.exclusion_reasons for item in incomplete.episodes)

    base = simple_long_request()
    trade_only = normalize_and_reconstruct(
        request_for(
            [
                trade_row("101", "BUY", "2", "10.00"),
                trade_row("102", "SELL", "2", "11.00", "2.00", time="2026-01-02 10:05:01"),
            ],
            [],
        )
    ).episodes[0]
    assert base.declared_position_mode == "ONE_WAY"
    assert trade_only.reconstruction_status is ReconstructionStatus.COMPLETE
    assert trade_only.reconciliation_status is ReconciliationStatus.SUMMARY_MISMATCH
    assert "POSITION_SUMMARY_MISSING" in trade_only.exclusion_reasons

    position_only = normalize_and_reconstruct(
        request_for([], [position_row("Long", "10", "11", "1", "1", "1")])
    ).episodes[0]
    assert position_only.reconstruction_status is ReconstructionStatus.INCONSISTENT
    assert position_only.allocations == ()
    assert "POSITION_SUMMARY_WITHOUT_TRADES" in position_only.exclusion_reasons


def test_trading_review_fee_rebate_and_unvalued_asset_capability() -> None:
    rebate = normalize_and_reconstruct(
        request_for(
            [
                trade_row("1", "BUY", "1", "10", fee="0.02USDT"),
                trade_row(
                    "2",
                    "SELL",
                    "1",
                    "12",
                    "2",
                    "-0.01USDT",
                    time="2026-01-02 10:05:01",
                ),
            ],
            [position_row("Long", "10", "12", "1", "1", "2")],
        )
    ).episodes[0]
    assert rebate.net_trading_pnl is not None
    assert rebate.net_trading_pnl.amount.value == Decimal("1.99")

    trades = [
        trade_row("1", "BUY", "1", "10", fee="0.001FEECOIN"),
        trade_row("2", "SELL", "1", "12", "2", time="2026-01-02 10:05:01"),
    ]
    positions = [position_row("Long", "10", "12", "1", "1", "2")]
    unvalued = normalize_and_reconstruct(request_for(trades, positions)).episodes[0]
    assert unvalued.eligible
    assert unvalued.net_trading_pnl is None
    assert unvalued.capability.unsupported_fee_assets == ("FEECOIN",)
    valuation = FeeValuation(
        source_asset="FEECOIN",
        settlement_asset="USDT",
        rate=ScaledDecimal.parse("2.00"),
        valuation_version="FEECOIN_USDT_V1",
    )
    valued = normalize_and_reconstruct(
        request_for(trades, positions, fee_valuations=(valuation,))
    ).episodes[0]
    assert valued.net_trading_pnl is not None
    assert valued.net_trading_pnl.amount.value == Decimal("1.998")


@pytest.mark.parametrize(
    ("trade_pnl", "expected_status", "eligible"),
    [
        ("2.004", ReconciliationStatus.WITHIN_ROUNDING_TOLERANCE, True),
        ("2.006", ReconciliationStatus.SUMMARY_MISMATCH, False),
    ],
)
def test_trading_review_derived_rounding_tolerance(
    trade_pnl: str, expected_status: ReconciliationStatus, eligible: bool
) -> None:
    result = normalize_and_reconstruct(
        request_for(
            [
                trade_row("1", "BUY", "1", "10.00", realized_pnl="0.000"),
                trade_row(
                    "2",
                    "SELL",
                    "1",
                    "12.00",
                    trade_pnl,
                    time="2026-01-02 10:05:01",
                ),
            ],
            [position_row("Long", "10.00", "12.00", "1", "1", "2.00")],
        )
    )
    episode = result.episodes[0]
    assert episode.reconciliation_status is expected_status
    assert episode.eligible is eligible
    delta = next(
        item
        for item in result.reconciliation_manifest.matches[0].deltas
        if item.field == "closingPnl"
    )
    assert delta.tolerance.value == Decimal("0.0055")


def test_trading_review_closing_pnl_excludes_fee() -> None:
    result = normalize_and_reconstruct(
        request_for(
            [
                trade_row("101", "BUY", "2", "10.00", fee="0.02USDT"),
                trade_row(
                    "102",
                    "SELL",
                    "2",
                    "11.00",
                    "2.00",
                    "0.02USDT",
                    time="2026-01-02 10:05:01",
                ),
            ],
            [position_row("Long", "10.00", "11.00", "2", "2", "2.00")],
        )
    )
    episode = result.episodes[0]
    assert episode.reconciliation_status is ReconciliationStatus.EXACT
    assert episode.gross_realized_pnl is not None
    assert episode.gross_realized_pnl.amount.value == Decimal("2.00")
    assert episode.net_trading_pnl is not None
    assert episode.net_trading_pnl.amount.value == Decimal("1.96")


def test_trading_review_unsupported_status_and_ambiguous_graph() -> None:
    trades = [
        trade_row("101", "BUY", "1", "10"),
        trade_row("102", "SELL", "1", "11", "1", time="2026-01-02 10:05:01"),
    ]
    unsupported = normalize_and_reconstruct(
        request_for(
            trades,
            [position_row("Long", "10", "11", "1", "1", "1", status="Open")],
        )
    ).episodes[0]
    assert unsupported.reconstruction_status is ReconstructionStatus.COMPLETE
    assert "UNSUPPORTED_POSITION_STATUS" in unsupported.exclusion_reasons

    same_time_trades = [
        trade_row("1601", "BUY", "1", "10", time="2026-01-02 10:00:01"),
        trade_row("1602", "SELL", "1", "11", "1", time="2026-01-02 10:00:01"),
        trade_row("1603", "BUY", "1", "10", time="2026-01-02 10:00:01"),
        trade_row("1604", "SELL", "1", "11", "1", time="2026-01-02 10:00:01"),
    ]
    summary = position_row(
        "Long",
        "10",
        "11",
        "1",
        "1",
        "1",
        opened="2026-01-02 10:00:01",
        closed="2026-01-02 10:00:01",
    )
    ambiguous = normalize_and_reconstruct(request_for(same_time_trades, [summary, summary.copy()]))
    assert all(
        item.reconciliation_status is ReconciliationStatus.AMBIGUOUS and not item.eligible
        for item in ambiguous.episodes
    )
    assert all(item.episode_id is None for item in ambiguous.reconciliation_manifest.matches)


def test_trading_review_hedge_overlap_rejected() -> None:
    trades = [
        trade_row("1", "BUY", "1", "10", time="2026-01-02 10:00:01"),
        trade_row("2", "SELL", "1", "11", "1", time="2026-01-02 10:05:01"),
        trade_row("3", "BUY", "1", "10", time="2026-01-02 10:10:01"),
    ]
    positions = [
        position_row("Long", "10", "10", "1", "1", "0", closed="2026-01-02 10:10:01"),
        position_row(
            "Short",
            "11",
            "10",
            "1",
            "1",
            "1",
            opened="2026-01-02 10:05:01",
            closed="2026-01-02 10:10:01",
        ),
    ]
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request_for(trades, positions))
    assert raised.value.code is FailureCode.UNSUPPORTED_POSITION_MODE


def test_trading_review_instrument_grid_violation_is_inconsistent() -> None:
    result = normalize_and_reconstruct(
        request_for(
            [
                trade_row("1", "BUY", "1.005", "10.00"),
                trade_row("2", "BUY", "0.005", "10.00", time="2026-01-02 10:01:01"),
                trade_row("3", "SELL", "1.01", "11.00", "1", time="2026-01-02 10:05:01"),
            ],
            [],
            quantity_step="0.01",
        )
    )
    assert result.episodes[0].reconstruction_status is ReconstructionStatus.INCONSISTENT
    assert "INSTRUMENT_RULE_VIOLATION" in result.episodes[0].exclusion_reasons


def test_trading_review_declared_mode_is_exact() -> None:
    request = replace(simple_long_request(), declared_position_mode="HEDGE")
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request)
    assert raised.value.code is FailureCode.UNSUPPORTED_POSITION_MODE


def test_trading_review_uniform_grid_contradiction_rejects_pinned_terms() -> None:
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(
            request_for(
                [
                    trade_row("1", "BUY", "1.005", "10.00"),
                    trade_row(
                        "2",
                        "SELL",
                        "1.005",
                        "11.00",
                        "1",
                        time="2026-01-02 10:05:01",
                    ),
                ],
                [],
                quantity_step="0.01",
            )
        )
    assert raised.value.code is FailureCode.UNSUPPORTED_PRODUCT


def test_trading_review_coverage_and_quality_are_role_independent() -> None:
    trades = [
        trade_row("1", "BUY", "1", "10", time="2026-01-02 09:00:01"),
        trade_row("2", "SELL", "1", "11", "1", time="2026-01-02 09:05:01"),
    ]
    positions = [
        position_row(
            "Long",
            "8",
            "9",
            "1",
            "1",
            "1",
            opened="2026-01-01 08:00:01",
            closed="2026-01-01 08:05:01",
        )
    ]
    result = normalize_and_reconstruct(request_for(trades, positions))
    coverages = {item.role.value: item for item in result.artifact_coverages}
    assert (
        coverages["TRADE_HISTORY"].earliest_observed_at
        != coverages["POSITION_HISTORY"].earliest_observed_at
    )
    assert all(item.completeness_assertion == "NOT_PROVEN" for item in coverages.values())
    quality = result.quality_report
    assert dict(quality.source_row_counts) == {
        next(role for role, _ in quality.source_row_counts if role.value == "POSITION_HISTORY"): 1,
        next(role for role, _ in quality.source_row_counts if role.value == "TRADE_HISTORY"): 2,
    }
    assert quality.canonical_record_count == 7
    assert quality.eligible_count == 0
    assert quality.excluded_count == 2


def test_trading_review_increase_while_losing_is_still_increase() -> None:
    result = normalize_and_reconstruct(
        request_for(
            [
                trade_row("1", "BUY", "2", "10", time="2026-01-02 10:00:01"),
                trade_row("2", "BUY", "1", "8", time="2026-01-02 10:01:01"),
                trade_row("3", "SELL", "3", "11", "5", time="2026-01-02 10:05:01"),
            ],
            [position_row("Long", "9.33", "11", "3", "3", "5")],
        )
    )
    assert [item.action.value for item in result.episodes[0].allocations] == [
        "OPEN",
        "INCREASE",
        "CLOSE",
    ]
    assert result.episodes[0].average_entry_price is not None
    assert result.episodes[0].average_entry_price.numerator == "28"
    assert result.episodes[0].average_entry_price.denominator == "3"
