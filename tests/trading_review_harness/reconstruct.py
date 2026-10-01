from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_DOWN, Decimal
from typing import Any

from tests.trading_review_harness.canonical import content_hash
from tests.trading_review_harness.model import (
    Allocation,
    Episode,
    Execution,
    InstrumentTerms,
    Provenance,
)


def _grid_valid(value: Decimal, quantum: Decimal) -> bool:
    if quantum <= 0:
        return False
    return value % quantum == 0


def _direction(position: Decimal) -> str:
    return "LONG" if position > 0 else "SHORT"


def _new_episode(temp_key: str, execution: Execution, quantity: Decimal) -> Episode:
    return Episode(
        key=temp_key,
        symbol=execution.symbol,
        direction="LONG" if execution.side == "BUY" else "SHORT",
        opened_at=execution.occurred_at,
        closed_at=None,
        initial_quantity=quantity,
        max_quantity=quantity,
        opening_volume=Decimal(0),
        closing_volume=Decimal(0),
        entry_numerator=Decimal(0),
        entry_denominator=Decimal(0),
        exit_numerator=Decimal(0),
        exit_denominator=Decimal(0),
        gross_pnl=Decimal(0),
        fees={},
        net_pnl=None,
        reconstruction_status="COMPLETE",
        reconciliation_status="INCOMPLETE_TRADES",
        capabilities=set(),
        eligible=False,
        exclusion_reasons=[],
        provenance=[],
    )


def _add_reason(episode: Episode, reason: str) -> None:
    if reason not in episode.exclusion_reasons:
        episode.exclusion_reasons.append(reason)


def _add_provenance(episode: Episode, values: list[Provenance]) -> None:
    existing = {item.reference() for item in episode.provenance}
    for value in values:
        if value.reference() not in existing:
            episode.provenance.append(value)
            existing.add(value.reference())


def _append_allocation(
    episode: Episode,
    allocations: list[Allocation],
    execution: Execution,
    action: str,
    quantity: Decimal,
    fee: Decimal,
    pnl: Decimal,
    reversal_group_id: str | None = None,
) -> None:
    allocation = Allocation(
        execution_key=execution.key,
        episode_key=episode.key,
        action=action,
        quantity=quantity,
        price=execution.price,
        fee_asset=execution.fee_asset,
        fee=fee,
        reported_pnl=pnl,
        provenance=list(execution.provenance),
        reversal_group_id=reversal_group_id,
    )
    allocations.append(allocation)
    episode.allocation_indexes.append(len(allocations) - 1)
    episode.fees[execution.fee_asset] = episode.fees.get(execution.fee_asset, Decimal(0)) + fee
    episode.gross_pnl = (episode.gross_pnl or Decimal(0)) + pnl
    _add_provenance(episode, execution.provenance)
    if action in {"OPEN", "INCREASE", "REVERSAL_OPEN"}:
        episode.opening_volume += quantity
        episode.entry_numerator = (
            episode.entry_numerator or Decimal(0)
        ) + quantity * execution.price
        episode.entry_denominator = (episode.entry_denominator or Decimal(0)) + quantity
    else:
        episode.closing_volume += quantity
        episode.exit_numerator = (episode.exit_numerator or Decimal(0)) + quantity * execution.price
        episode.exit_denominator = (episode.exit_denominator or Decimal(0)) + quantity


def _split_fee(fee: Decimal, closing: Decimal, total: Decimal) -> tuple[Decimal, Decimal]:
    quantum = Decimal(1).scaleb(fee.as_tuple().exponent)
    closing_share = (fee * closing / total).quantize(quantum, rounding=ROUND_DOWN)
    return closing_share, fee - closing_share


def _finalize_keys(
    episodes: list[Episode], allocations: list[Allocation], policy_version: str
) -> None:
    for episode in episodes:
        identity: list[dict[str, Any]] = []
        for index in episode.allocation_indexes:
            allocation = allocations[index]
            identity.append(
                {
                    "action": allocation.action,
                    "executionKey": allocation.execution_key,
                    "quantity": str(allocation.quantity),
                }
            )
        old_key = episode.key
        episode.key = (
            "ep-"
            + content_hash(
                {
                    "allocations": identity,
                    "direction": episode.direction,
                    "policyVersion": policy_version,
                    "symbol": episode.symbol,
                }
            )[:20]
        )
        for allocation in allocations:
            if allocation.episode_key == old_key:
                allocation.episode_key = episode.key


def reconstruct(
    executions: list[Execution],
    terms_by_symbol: dict[str, InstrumentTerms],
    policy_version: str,
    fee_valuations: dict[str, Decimal],
) -> tuple[list[Episode], list[Allocation]]:
    episodes: list[Episode] = []
    allocations: list[Allocation] = []
    executions_by_symbol: dict[str, list[Execution]] = defaultdict(list)
    for execution in executions:
        executions_by_symbol[execution.symbol].append(execution)

    for symbol in sorted(executions_by_symbol):
        ordered = sorted(
            executions_by_symbol[symbol],
            key=lambda item: (item.occurred_at, int(item.raw_trade_id)),
        )
        terms = terms_by_symbol[symbol]
        position = Decimal(0)
        active: Episode | None = None
        contaminated = False
        for execution in ordered:
            delta = execution.quantity if execution.side == "BUY" else -execution.quantity
            next_position = position + delta
            grid_invalid = not _grid_valid(execution.quantity, terms.step_size) or not _grid_valid(
                execution.price, terms.tick_size
            )
            if position == 0:
                active = _new_episode(f"tmp-{len(episodes) + 1}", execution, abs(next_position))
                episodes.append(active)
                _append_allocation(
                    active,
                    allocations,
                    execution,
                    "OPEN",
                    execution.quantity,
                    execution.fee,
                    execution.realized_pnl,
                )
                if execution.realized_pnl != 0:
                    active.reconstruction_status = "INCONSISTENT"
                    _add_reason(active, "MISSING_EXECUTION_SUSPECTED")
                    contaminated = True
                if grid_invalid:
                    active.reconstruction_status = "INCONSISTENT"
                    _add_reason(active, "INSTRUMENT_RULE_VIOLATION")
                    contaminated = True
            elif active is None:
                raise AssertionError("non-zero position requires an active episode")
            elif position * delta > 0:
                _append_allocation(
                    active,
                    allocations,
                    execution,
                    "INCREASE",
                    execution.quantity,
                    execution.fee,
                    execution.realized_pnl,
                )
                active.max_quantity = max(active.max_quantity or Decimal(0), abs(next_position))
                if execution.realized_pnl != 0:
                    active.reconstruction_status = "INCONSISTENT"
                    _add_reason(active, "MISSING_EXECUTION_SUSPECTED")
                    contaminated = True
            elif next_position == 0:
                _append_allocation(
                    active,
                    allocations,
                    execution,
                    "CLOSE",
                    abs(position),
                    execution.fee,
                    execution.realized_pnl,
                )
                active.closed_at = execution.occurred_at
                if contaminated or grid_invalid:
                    active.reconstruction_status = "INCONSISTENT"
                    if grid_invalid:
                        _add_reason(active, "INSTRUMENT_RULE_VIOLATION")
                else:
                    active.reconstruction_status = "COMPLETE"
                active = None
            elif position * next_position > 0:
                _append_allocation(
                    active,
                    allocations,
                    execution,
                    "REDUCE",
                    execution.quantity,
                    execution.fee,
                    execution.realized_pnl,
                )
                if grid_invalid:
                    active.reconstruction_status = "INCONSISTENT"
                    _add_reason(active, "INSTRUMENT_RULE_VIOLATION")
                    contaminated = True
            else:
                closing_quantity = abs(position)
                opening_quantity = abs(next_position)
                closing_fee, opening_fee = _split_fee(
                    execution.fee, closing_quantity, execution.quantity
                )
                reversal_group_id = content_hash(
                    {
                        "executionKey": execution.key,
                        "policyVersion": policy_version,
                    }
                )
                _append_allocation(
                    active,
                    allocations,
                    execution,
                    "REVERSAL_CLOSE",
                    closing_quantity,
                    closing_fee,
                    execution.realized_pnl,
                    reversal_group_id,
                )
                active.closed_at = execution.occurred_at
                if contaminated or grid_invalid:
                    active.reconstruction_status = "INCONSISTENT"
                    if grid_invalid:
                        _add_reason(active, "INSTRUMENT_RULE_VIOLATION")
                else:
                    active.reconstruction_status = "COMPLETE"
                active = _new_episode(f"tmp-{len(episodes) + 1}", execution, opening_quantity)
                episodes.append(active)
                _append_allocation(
                    active,
                    allocations,
                    execution,
                    "REVERSAL_OPEN",
                    opening_quantity,
                    opening_fee,
                    Decimal(0).quantize(execution.realized_pnl),
                    reversal_group_id,
                )
                if contaminated or grid_invalid:
                    active.reconstruction_status = "INCONSISTENT"
                    if grid_invalid:
                        _add_reason(active, "INSTRUMENT_RULE_VIOLATION")
            position = next_position

        if active is not None:
            _add_reason(active, "EPISODE_RIGHT_CENSORED")
            if active.reconstruction_status != "INCONSISTENT":
                active.reconstruction_status = "RIGHT_CENSORED"
            active.closed_at = None

    _finalize_keys(episodes, allocations, policy_version)
    for episode in episodes:
        if episode.entry_denominator == 0:
            episode.entry_numerator = None
            episode.entry_denominator = None
        if episode.exit_denominator == 0:
            episode.exit_numerator = None
            episode.exit_denominator = None
        episode.capabilities.update(
            {
                "FEES",
                "POSITION_MODE_USER_DECLARED",
                "VENUE_REPORTED_PNL",
            }
        )
        if episode.initial_quantity is not None:
            episode.capabilities.add("INITIAL_EXPOSURE")
        if episode.max_quantity is not None:
            episode.capabilities.add("MAX_EXPOSURE")
        if episode.reconstruction_status != "INCONSISTENT":
            episode.capabilities.add("POSITION_INCREASES")
        settlement_asset = terms_by_symbol[episode.symbol].settlement_asset
        converted_fees = Decimal(0)
        all_valued = True
        for asset, fee in episode.fees.items():
            if asset == settlement_asset:
                converted_fees += fee
            elif asset in fee_valuations:
                converted_fees += fee * fee_valuations[asset]
            else:
                all_valued = False
        if all_valued and episode.gross_pnl is not None:
            episode.net_pnl = episode.gross_pnl - converted_fees
        episode.exclusion_reasons.sort()
        episode.provenance.sort(key=lambda item: item.reference())
    episodes.sort(
        key=lambda item: (item.symbol, item.opened_at or ordered[0].occurred_at, item.key)
    )
    allocations.sort(
        key=lambda item: (
            item.execution_key.split(":", maxsplit=1)[0],
            next(
                execution.occurred_at
                for execution in executions
                if execution.key == item.execution_key
            ),
            int(item.execution_key.rsplit(":", maxsplit=1)[1]),
            0 if item.action == "REVERSAL_CLOSE" else 1,
        )
    )
    return episodes, allocations
