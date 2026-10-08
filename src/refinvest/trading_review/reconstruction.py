"""Deterministic one-way signed-position reconstruction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from refinvest.trading_review.decimal import ExactRational, ScaledDecimal
from refinvest.trading_review.hashing import canonical_tuple, sha256_hex, stable_id
from refinvest.trading_review.model import (
    AllocationAction,
    Capability,
    CapabilitySnapshot,
    Direction,
    ExecutionRecord,
    FeeRecord,
    FeeValuation,
    FuturesPositionEpisode,
    InstrumentDescriptor,
    MoneyAmount,
    PositionAllocation,
    ReconciliationStatus,
    ReconstructionStatus,
    Side,
    VenueReportedPnlRecord,
)


@dataclass(slots=True)
class _EpisodeBuilder:
    episode_id: str
    instrument: InstrumentDescriptor
    source_symbol: str
    direction: Direction
    opened_at: datetime
    initial_quantity: Decimal
    max_quantity: Decimal
    allocation_quantities: list[ScaledDecimal] = field(default_factory=list[ScaledDecimal])
    opening_volume: Decimal = Decimal(0)
    closing_volume: Decimal = Decimal(0)
    entry_weighted_sum: Decimal = Decimal(0)
    exit_weighted_sum: Decimal = Decimal(0)
    gross_pnl: Decimal = Decimal(0)
    gross_pnl_scale: int = 0
    fees: dict[str, Decimal] = field(default_factory=dict[str, Decimal])
    fee_scales: dict[str, int] = field(default_factory=dict[str, int])
    allocations: list[PositionAllocation] = field(default_factory=list[PositionAllocation])
    evidence_record_ids: set[str] = field(default_factory=set[str])
    status: ReconstructionStatus = ReconstructionStatus.COMPLETE
    exclusion_reasons: set[str] = field(default_factory=set[str])
    closed_at: datetime | None = None
    entry_price_scale: int = 0
    exit_price_scale: int = 0


def reconstruct_positions(
    executions: tuple[ExecutionRecord, ...],
    fees: tuple[FeeRecord, ...],
    pnl_records: tuple[VenueReportedPnlRecord, ...],
    instruments: tuple[InstrumentDescriptor, ...],
    *,
    reconstruction_policy_version: str,
    fee_valuations: tuple[FeeValuation, ...] = (),
) -> tuple[FuturesPositionEpisode, ...]:
    fee_by_key = {record.source_execution_key: record for record in fees}
    pnl_by_key = {record.source_execution_key: record for record in pnl_records}
    terms_by_id = {instrument.instrument_id: instrument for instrument in instruments}
    valuations = {(item.source_asset, item.settlement_asset): item for item in fee_valuations}
    by_instrument: dict[str, list[ExecutionRecord]] = {}
    for execution in executions:
        by_instrument.setdefault(execution.instrument_id, []).append(execution)
    completed: list[FuturesPositionEpisode] = []
    for instrument_id in sorted(by_instrument):
        terms = terms_by_id[instrument_id]
        position = Decimal(0)
        current: _EpisodeBuilder | None = None
        contaminated = False
        for execution in by_instrument[instrument_id]:
            fee = fee_by_key[execution.source_execution_key]
            pnl = pnl_by_key[execution.source_execution_key]
            delta = (
                execution.quantity.value
                if execution.side is Side.BUY
                else -execution.quantity.value
            )
            next_position = position + delta
            grid_valid = _on_grid(execution.quantity.value, terms.quantity_step.value) and _on_grid(
                execution.price.value, terms.price_tick.value
            )
            if position == 0:
                current = _new_builder(
                    execution,
                    abs(next_position),
                    terms,
                    reconstruction_policy_version,
                )
                action = AllocationAction.OPEN
                if pnl.realized_pnl.value != 0:
                    contaminated = True
                    _mark_inconsistent(current, "MISSING_EXECUTION_SUSPECTED")
                if contaminated:
                    _mark_inconsistent(current, "MISSING_EXECUTION_SUSPECTED")
                if not grid_valid:
                    _mark_inconsistent(current, "INSTRUMENT_RULE_VIOLATION")
                allocation = _allocation(
                    execution,
                    fee,
                    pnl,
                    current.episode_id,
                    action,
                    execution.quantity,
                    fee.source_amount,
                    pnl.realized_pnl,
                )
                _append_allocation(current, allocation, execution, opening=True)
            elif _same_sign(position, delta):
                assert current is not None
                action = AllocationAction.INCREASE
                if pnl.realized_pnl.value != 0:
                    contaminated = True
                    _mark_inconsistent(current, "MISSING_EXECUTION_SUSPECTED")
                if not grid_valid:
                    _mark_inconsistent(current, "INSTRUMENT_RULE_VIOLATION")
                allocation = _allocation(
                    execution,
                    fee,
                    pnl,
                    current.episode_id,
                    action,
                    execution.quantity,
                    fee.source_amount,
                    pnl.realized_pnl,
                )
                _append_allocation(current, allocation, execution, opening=True)
                current.max_quantity = max(current.max_quantity, abs(next_position))
            elif next_position == 0 or _same_sign(position, next_position):
                assert current is not None
                action = AllocationAction.CLOSE if next_position == 0 else AllocationAction.REDUCE
                if not grid_valid:
                    _mark_inconsistent(current, "INSTRUMENT_RULE_VIOLATION")
                allocation = _allocation(
                    execution,
                    fee,
                    pnl,
                    current.episode_id,
                    action,
                    execution.quantity,
                    fee.source_amount,
                    pnl.realized_pnl,
                )
                _append_allocation(current, allocation, execution, opening=False)
                if next_position == 0:
                    current.closed_at = execution.occurred_at
                    completed.append(_finalize(current, valuations))
                    current = None
            else:
                assert current is not None
                closing_quantity = abs(position)
                opening_quantity = abs(next_position)
                reversal_group = "reversal_" + sha256_hex(
                    canonical_tuple(
                        execution.source_execution_key,
                        reconstruction_policy_version,
                    )
                )
                closing_fee, opening_fee = _split_fee(
                    fee.source_amount, closing_quantity, execution.quantity.value
                )
                closing_allocation = _allocation(
                    execution,
                    fee,
                    pnl,
                    current.episode_id,
                    AllocationAction.REVERSAL_CLOSE,
                    ScaledDecimal.from_decimal(closing_quantity),
                    closing_fee,
                    pnl.realized_pnl,
                    reversal_group,
                )
                if not grid_valid:
                    _mark_inconsistent(current, "INSTRUMENT_RULE_VIOLATION")
                _append_allocation(current, closing_allocation, execution, opening=False)
                current.closed_at = execution.occurred_at
                completed.append(_finalize(current, valuations))
                current = _new_builder(
                    execution,
                    opening_quantity,
                    terms,
                    reconstruction_policy_version,
                    reversal_open=True,
                )
                if contaminated:
                    _mark_inconsistent(current, "MISSING_EXECUTION_SUSPECTED")
                if not grid_valid:
                    _mark_inconsistent(current, "INSTRUMENT_RULE_VIOLATION")
                opening_allocation = _allocation(
                    execution,
                    fee,
                    pnl,
                    current.episode_id,
                    AllocationAction.REVERSAL_OPEN,
                    ScaledDecimal.from_decimal(opening_quantity),
                    opening_fee,
                    ScaledDecimal.zero(pnl.realized_pnl.scale),
                    reversal_group,
                )
                _append_allocation(current, opening_allocation, execution, opening=True)
            position = next_position
        if current is not None:
            current.exclusion_reasons.add("EPISODE_RIGHT_CENSORED")
            if current.status is ReconstructionStatus.COMPLETE:
                current.status = ReconstructionStatus.RIGHT_CENSORED
            completed.append(_finalize(current, valuations))
    return tuple(
        sorted(
            completed,
            key=lambda item: (
                item.instrument_id,
                item.opened_at is None,
                item.opened_at,
                item.episode_id,
            ),
        )
    )


def _new_builder(
    execution: ExecutionRecord,
    quantity: Decimal,
    instrument: InstrumentDescriptor,
    policy_version: str,
    *,
    reversal_open: bool = False,
) -> _EpisodeBuilder:
    direction = Direction.LONG if execution.side is Side.BUY else Direction.SHORT
    episode_id = stable_id(
        "episode",
        {
            "policyVersion": policy_version,
            "instrumentId": execution.instrument_id,
            "sourceExecutionKey": execution.source_execution_key,
            "openingAction": "REVERSAL_OPEN" if reversal_open else "OPEN",
            "direction": direction,
        },
    )
    return _EpisodeBuilder(
        episode_id=episode_id,
        instrument=instrument,
        source_symbol=execution.source_symbol,
        direction=direction,
        opened_at=execution.occurred_at,
        initial_quantity=quantity,
        max_quantity=quantity,
    )


def _allocation(
    execution: ExecutionRecord,
    fee: FeeRecord,
    pnl: VenueReportedPnlRecord,
    episode_id: str,
    action: AllocationAction,
    quantity: ScaledDecimal,
    allocated_fee: ScaledDecimal,
    allocated_pnl: ScaledDecimal,
    reversal_group_id: str | None = None,
) -> PositionAllocation:
    return PositionAllocation(
        execution_record_id=execution.record_id,
        source_execution_key=execution.source_execution_key,
        episode_id=episode_id,
        action=action,
        allocated_quantity=quantity,
        allocated_fee=MoneyAmount(allocated_fee, fee.asset),
        allocated_reported_pnl=MoneyAmount(allocated_pnl, pnl.settlement_asset),
        reversal_group_id=reversal_group_id,
        evidence_record_ids=(execution.record_id, fee.record_id, pnl.record_id),
    )


def _append_allocation(
    builder: _EpisodeBuilder,
    allocation: PositionAllocation,
    execution: ExecutionRecord,
    *,
    opening: bool,
) -> None:
    builder.allocations.append(allocation)
    builder.allocation_quantities.append(allocation.allocated_quantity)
    builder.evidence_record_ids.update(allocation.evidence_record_ids)
    quantity = allocation.allocated_quantity.value
    if opening:
        builder.opening_volume += quantity
        builder.entry_weighted_sum += quantity * execution.price.value
        builder.entry_price_scale = max(builder.entry_price_scale, execution.price.scale)
    else:
        builder.closing_volume += quantity
        builder.exit_weighted_sum += quantity * execution.price.value
        builder.exit_price_scale = max(builder.exit_price_scale, execution.price.scale)
    fee = allocation.allocated_fee
    builder.fees[fee.asset] = builder.fees.get(fee.asset, Decimal(0)) + fee.amount.value
    builder.fee_scales[fee.asset] = max(builder.fee_scales.get(fee.asset, 0), fee.amount.scale)
    pnl = allocation.allocated_reported_pnl.amount
    builder.gross_pnl += pnl.value
    builder.gross_pnl_scale = max(builder.gross_pnl_scale, pnl.scale)


def _finalize(
    builder: _EpisodeBuilder,
    valuations: dict[tuple[str, str], FeeValuation],
) -> FuturesPositionEpisode:
    quantity_scale = max((value.scale for value in builder.allocation_quantities), default=0)
    opening_volume = ScaledDecimal.from_decimal(builder.opening_volume, scale=quantity_scale)
    closing_volume = ScaledDecimal.from_decimal(builder.closing_volume, scale=quantity_scale)
    initial_quantity = ScaledDecimal.from_decimal(builder.initial_quantity, scale=quantity_scale)
    max_quantity = ScaledDecimal.from_decimal(builder.max_quantity, scale=quantity_scale)
    average_entry = (
        ExactRational.divide(builder.entry_weighted_sum, builder.opening_volume)
        if builder.opening_volume != 0
        else None
    )
    average_exit = (
        ExactRational.divide(builder.exit_weighted_sum, builder.closing_volume)
        if builder.closing_volume != 0
        else None
    )
    gross = MoneyAmount(
        ScaledDecimal.from_decimal(builder.gross_pnl, scale=builder.gross_pnl_scale),
        builder.instrument.settlement_asset,
    )
    fees = tuple(
        MoneyAmount(
            ScaledDecimal.from_decimal(value, scale=builder.fee_scales[asset]),
            asset,
        )
        for asset, value in sorted(builder.fees.items())
    )
    unsupported_fee_assets: list[str] = []
    converted_fee = Decimal(0)
    for fee in fees:
        if fee.asset == builder.instrument.settlement_asset:
            converted_fee += fee.amount.value
            continue
        valuation = valuations.get((fee.asset, builder.instrument.settlement_asset))
        if valuation is None:
            unsupported_fee_assets.append(fee.asset)
        else:
            converted_fee += fee.amount.value * valuation.rate.value
    complete = builder.status is ReconstructionStatus.COMPLETE
    net = (
        MoneyAmount(
            ScaledDecimal.from_decimal(builder.gross_pnl - converted_fee),
            builder.instrument.settlement_asset,
        )
        if complete and not unsupported_fee_assets
        else None
    )
    capabilities = {
        Capability.FEES,
        Capability.VENUE_REPORTED_PNL,
        Capability.INITIAL_EXPOSURE,
        Capability.MAX_EXPOSURE,
        Capability.POSITION_INCREASES,
        Capability.POSITION_MODE_USER_DECLARED,
        Capability.POSITION_MODE_OBSERVED_COMPATIBLE,
    }
    if complete and builder.closed_at is not None:
        capabilities.add(Capability.STABLE_EPISODE_TIME)
    snapshot = CapabilitySnapshot(
        capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
        gross_pnl_available=complete,
        fee_adjusted_outcome_available=False,
        holding_duration_available=complete and builder.closed_at is not None,
        stable_episode_time_available=complete and builder.closed_at is not None,
        reconciliation_available=False,
        funding_inclusive_outcome_available=False,
        unsupported_fee_assets=tuple(sorted(unsupported_fee_assets)),
    )
    return FuturesPositionEpisode(
        episode_id=builder.episode_id,
        instrument_id=builder.instrument.instrument_id,
        source_symbol=builder.source_symbol,
        direction=builder.direction,
        opened_at=builder.opened_at,
        closed_at=builder.closed_at,
        initial_quantity=initial_quantity,
        max_open_quantity=max_quantity,
        opening_volume=opening_volume,
        closing_volume=closing_volume,
        average_entry_price=average_entry,
        average_close_price=average_exit,
        gross_realized_pnl=gross,
        fees_by_asset=fees,
        net_trading_pnl=net,
        reconstruction_status=builder.status,
        reconciliation_status=ReconciliationStatus.INCOMPLETE_TRADES,
        allocations=tuple(builder.allocations),
        capability=snapshot,
        eligible=False,
        exclusion_reasons=tuple(sorted(builder.exclusion_reasons)),
        evidence_record_ids=tuple(sorted(builder.evidence_record_ids)),
        _entry_price_source_scale=builder.entry_price_scale,
        _exit_price_source_scale=builder.exit_price_scale,
    )


def _split_fee(
    fee: ScaledDecimal,
    closing_quantity: Decimal,
    execution_quantity: Decimal,
) -> tuple[ScaledDecimal, ScaledDecimal]:
    quantum = Decimal(1).scaleb(-fee.scale)
    closing = (fee.value * closing_quantity / execution_quantity).quantize(
        quantum, rounding=ROUND_DOWN
    )
    opening = fee.value - closing
    return (
        ScaledDecimal.from_decimal(closing, scale=fee.scale),
        ScaledDecimal.from_decimal(opening, scale=fee.scale),
    )


def _mark_inconsistent(builder: _EpisodeBuilder, reason: str) -> None:
    builder.status = ReconstructionStatus.INCONSISTENT
    builder.exclusion_reasons.add(reason)


def _on_grid(value: Decimal, quantum: Decimal) -> bool:
    return quantum > 0 and value % quantum == 0


def _same_sign(left: Decimal, right: Decimal) -> bool:
    return (left > 0 and right > 0) or (left < 0 and right < 0)
