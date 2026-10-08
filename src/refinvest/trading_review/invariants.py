"""Conservation and eligibility invariants for reconstructed manifests."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from refinvest.trading_review.errors import FailureCode, FailureStage, fail
from refinvest.trading_review.model import (
    AllocationAction,
    Capability,
    ExecutionRecord,
    FeeRecord,
    FuturesPositionEpisode,
    PositionAllocation,
    ReconciliationStatus,
    ReconstructionStatus,
    VenuePositionSummaryRecord,
    VenueReportedPnlRecord,
)


def verify_conservation(
    executions: tuple[ExecutionRecord, ...],
    fees: tuple[FeeRecord, ...],
    pnl_records: tuple[VenueReportedPnlRecord, ...],
    episodes: tuple[FuturesPositionEpisode, ...],
    summaries: tuple[VenuePositionSummaryRecord, ...] = (),
) -> tuple[bool, bool, bool]:
    allocations_by_execution: dict[str, list[PositionAllocation]] = defaultdict(list)
    for episode in episodes:
        for allocation in episode.allocations:
            allocations_by_execution[allocation.source_execution_key].append(allocation)
            if allocation.episode_id != episode.episode_id or not allocation.evidence_record_ids:
                _invariant_failure("ALLOCATION_MEMBERSHIP_OR_EVIDENCE")
        if episode.reconstruction_status is ReconstructionStatus.COMPLETE:
            if episode.opening_volume.value != episode.closing_volume.value:
                _invariant_failure("COMPLETE_EPISODE_VOLUME")
        if episode.eligible and (
            episode.reconstruction_status is not ReconstructionStatus.COMPLETE
            or episode.reconciliation_status
            not in {
                ReconciliationStatus.EXACT,
                ReconciliationStatus.WITHIN_ROUNDING_TOLERANCE,
            }
            or not episode.evidence_record_ids
            or Capability.VENUE_POSITION_SUMMARY not in episode.capability.capabilities
        ):
            _invariant_failure("INELIGIBLE_EPISODE_PROMOTION")

    fee_by_key = {item.source_execution_key: item for item in fees}
    pnl_by_key = {item.source_execution_key: item for item in pnl_records}
    for execution in executions:
        linked = allocations_by_execution[execution.source_execution_key]
        if (
            sum((item.allocated_quantity.value for item in linked), Decimal(0))
            != execution.quantity.value
        ):
            _invariant_failure("EXECUTION_QUANTITY_CONSERVATION")
        fee = fee_by_key[execution.source_execution_key]
        linked_fee = [item for item in linked if item.allocated_fee.asset == fee.asset]
        if (
            len(linked_fee) != len(linked)
            or sum((item.allocated_fee.amount.value for item in linked_fee), Decimal(0))
            != fee.source_amount.value
        ):
            _invariant_failure("FEE_CONSERVATION")
        pnl = pnl_by_key[execution.source_execution_key]
        if (
            sum((item.allocated_reported_pnl.amount.value for item in linked), Decimal(0))
            != pnl.realized_pnl.value
        ):
            _invariant_failure("REPORTED_PNL_CONSERVATION")
        if len(linked) > 1:
            reversal_groups = {item.reversal_group_id for item in linked}
            actions = {item.action for item in linked}
            if (
                len(linked) != 2
                or None in reversal_groups
                or len(reversal_groups) != 1
                or actions != {AllocationAction.REVERSAL_CLOSE, AllocationAction.REVERSAL_OPEN}
                or len({item.episode_id for item in linked}) != 2
            ):
                _invariant_failure("REVERSAL_ALLOCATION_DISJOINTNESS")
    if any(not record.provenance for record in (*executions, *fees, *pnl_records, *summaries)):
        _invariant_failure("SOURCE_PROVENANCE_COMPLETENESS")
    return True, True, True


def _invariant_failure(reason: str) -> None:
    raise fail(
        FailureCode.INTERNAL_ERROR,
        FailureStage.CONSERVATION,
        reason=reason,
    )
