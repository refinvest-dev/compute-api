"""Constraint-graph reconciliation against venue position summaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction

from refinvest.trading_review.decimal import ExactRational, ScaledDecimal, source_bound
from refinvest.trading_review.hashing import stable_id
from refinvest.trading_review.model import (
    Capability,
    CapabilitySnapshot,
    ExecutionRecord,
    FuturesPositionEpisode,
    InstrumentDescriptor,
    ReconciliationDelta,
    ReconciliationIssue,
    ReconciliationMatch,
    ReconciliationStatus,
    ReconstructionStatus,
    VenuePositionSummaryRecord,
)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    episodes: tuple[FuturesPositionEpisode, ...]
    matches: tuple[ReconciliationMatch, ...]
    issues: tuple[ReconciliationIssue, ...]


@dataclass(frozen=True, slots=True)
class _Evaluation:
    deltas: tuple[ReconciliationDelta, ...]
    primary_within: bool
    all_primary_exact: bool
    secondary_within: bool
    all_secondary_exact: bool


def reconcile_positions(
    episodes: tuple[FuturesPositionEpisode, ...],
    summaries: tuple[VenuePositionSummaryRecord, ...],
    instruments: tuple[InstrumentDescriptor, ...],
    executions: tuple[ExecutionRecord, ...],
) -> ReconciliationResult:
    terms = {item.instrument_id: item for item in instruments}
    complete = {
        item.episode_id: item
        for item in episodes
        if item.reconstruction_status is ReconstructionStatus.COMPLETE
        and item.opened_at is not None
        and item.closed_at is not None
    }
    supported = {item.record_id: item for item in summaries if item.source_status == "Closed"}
    evaluations: dict[tuple[str, str], _Evaluation] = {}
    structural_pairs: set[tuple[str, str]] = set()
    primary_edges: set[tuple[str, str]] = set()
    for episode in complete.values():
        for summary in supported.values():
            if not _structurally_equal(episode, summary):
                continue
            pair = (episode.episode_id, summary.record_id)
            structural_pairs.add(pair)
            evaluation = _evaluate(episode, summary, terms[episode.instrument_id])
            evaluations[pair] = evaluation
            if evaluation.primary_within:
                primary_edges.add(pair)
    final_edges = set(primary_edges)
    secondary_used_pairs: set[tuple[str, str]] = set()
    for episode_ids, summary_ids in _components(primary_edges):
        component_edges = {
            pair for pair in primary_edges if pair[0] in episode_ids and pair[1] in summary_ids
        }
        if len(component_edges) > 1:
            final_edges.difference_update(component_edges)
            retained = {pair for pair in component_edges if evaluations[pair].secondary_within}
            final_edges.update(retained)
            secondary_used_pairs.update(retained)

    matched: dict[str, str] = {}
    ambiguous_episodes: set[str] = set()
    ambiguous_summaries: set[str] = set()
    for episode_ids, summary_ids in _components(final_edges):
        component_edges = {
            pair for pair in final_edges if pair[0] in episode_ids and pair[1] in summary_ids
        }
        perfect = _perfect_matchings(episode_ids, summary_ids, component_edges)
        if len(perfect) == 1:
            matched.update(perfect[0])
        elif len(perfect) > 1:
            ambiguous_episodes.update(episode_ids)
            ambiguous_summaries.update(summary_ids)
        else:
            ambiguous_episodes.update(
                episode_id
                for episode_id in episode_ids
                if sum(pair[0] == episode_id for pair in component_edges) > 1
            )
            ambiguous_summaries.update(
                summary_id
                for summary_id in summary_ids
                if sum(pair[1] == summary_id for pair in component_edges) > 1
            )

    incomplete_links = _incomplete_summary_links(episodes, supported)
    updated: list[FuturesPositionEpisode] = []
    issues: list[ReconciliationIssue] = []
    matched_summaries = {*matched.values(), *incomplete_links.values()}
    for episode in episodes:
        if episode.episode_id in matched:
            summary_id = matched[episode.episode_id]
            evaluation = evaluations[(episode.episode_id, summary_id)]
            exact = evaluation.all_primary_exact and (
                (episode.episode_id, summary_id) not in secondary_used_pairs
                or evaluation.all_secondary_exact
            )
            status = (
                ReconciliationStatus.EXACT
                if exact
                else ReconciliationStatus.WITHIN_ROUNDING_TOLERANCE
            )
            updated.append(_eligible_episode(episode, summary_id, status))
            if status is ReconciliationStatus.WITHIN_ROUNDING_TOLERANCE:
                issues.append(
                    _issue("RECONCILIATION_WITHIN_ROUNDING_TOLERANCE", episode, summary_id)
                )
        elif episode.episode_id in ambiguous_episodes:
            reason = "AMBIGUOUS_POSITION_SUMMARY"
            updated.append(_excluded_episode(episode, ReconciliationStatus.AMBIGUOUS, reason))
            issues.append(_issue(reason, episode, None))
        elif episode.reconstruction_status is ReconstructionStatus.COMPLETE:
            related = [pair for pair in structural_pairs if pair[0] == episode.episode_id]
            if related and any(_pnl_outside(evaluations[pair]) for pair in related):
                reason = "PNL_MISMATCH"
            elif any(
                summary.instrument_id == episode.instrument_id
                and summary.direction is episode.direction
                and summary.opened_at == episode.opened_at
                and summary.closed_at == episode.closed_at
                and summary.source_status != "Closed"
                for summary in summaries
            ):
                reason = "UNSUPPORTED_POSITION_STATUS"
            else:
                reason = "POSITION_SUMMARY_MISSING"
            updated.append(
                _excluded_episode(episode, ReconciliationStatus.SUMMARY_MISMATCH, reason)
            )
            issues.append(_issue(reason, episode, None))
        else:
            linked_summary_id = incomplete_links.get(episode.episode_id)
            if linked_summary_id is None:
                updated.append(
                    _excluded_episode(
                        episode,
                        ReconciliationStatus.INCOMPLETE_TRADES,
                        None,
                    )
                )
            else:
                summary = supported[linked_summary_id]
                missing_execution = summary.closed_volume.value > episode.closing_volume.value or (
                    episode.max_open_quantity is not None
                    and summary.max_open_interest.value > episode.max_open_quantity.value
                )
                reasons = set(episode.exclusion_reasons)
                if missing_execution:
                    reasons.add("MISSING_EXECUTION_SUSPECTED")
                updated.append(
                    replace(
                        episode,
                        reconstruction_status=(
                            ReconstructionStatus.INCONSISTENT
                            if missing_execution
                            else episode.reconstruction_status
                        ),
                        reconciliation_status=ReconciliationStatus.INCOMPLETE_TRADES,
                        eligible=False,
                        exclusion_reasons=tuple(sorted(reasons)),
                        evidence_record_ids=tuple(
                            sorted({*episode.evidence_record_ids, linked_summary_id})
                        ),
                    )
                )
                if missing_execution:
                    issues.append(_issue("MISSING_EXECUTION_SUSPECTED", episode, linked_summary_id))

    existing_ids = {item.episode_id for item in updated}
    for summary in supported.values():
        if summary.record_id in matched_summaries or summary.record_id in ambiguous_summaries:
            continue
        same_instrument = [
            execution
            for execution in executions
            if execution.instrument_id == summary.instrument_id
        ]
        earliest = min((item.occurred_at for item in same_instrument), default=None)
        status = (
            ReconstructionStatus.LEFT_CENSORED
            if earliest is not None and summary.opened_at < earliest
            else ReconstructionStatus.INCONSISTENT
        )
        reason = (
            "EPISODE_LEFT_CENSORED"
            if status is ReconstructionStatus.LEFT_CENSORED
            else "POSITION_SUMMARY_WITHOUT_TRADES"
        )
        shell = _summary_shell(summary, status, reason)
        if shell.episode_id not in existing_ids:
            updated.append(shell)
            existing_ids.add(shell.episode_id)
        issues.append(_issue(reason, shell, summary.record_id))

    matches = tuple(
        _summary_match(
            summary,
            matched,
            ambiguous_summaries,
            incomplete_links,
            final_edges,
            evaluations,
            secondary_used_pairs,
        )
        for summary in sorted(summaries, key=lambda item: item.source_identity)
    )
    return ReconciliationResult(
        episodes=tuple(
            sorted(
                updated,
                key=lambda item: (
                    item.instrument_id,
                    item.opened_at is None,
                    item.opened_at,
                    item.episode_id,
                ),
            )
        ),
        matches=matches,
        issues=tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.code,
                    item.episode_id or "",
                    item.summary_record_id or "",
                ),
            )
        ),
    )


def has_unsupported_overlap(
    summaries: tuple[VenuePositionSummaryRecord, ...],
    executions: tuple[ExecutionRecord, ...],
) -> bool:
    execution_times = {(item.instrument_id, item.occurred_at) for item in executions}
    supported = [item for item in summaries if item.source_status == "Closed"]
    for index, left in enumerate(supported):
        for right in supported[index + 1 :]:
            if left.instrument_id != right.instrument_id or left.direction is right.direction:
                continue
            overlap = max(left.opened_at, right.opened_at) < min(left.closed_at, right.closed_at)
            boundaries_observed = all(
                (left.instrument_id, instant) in execution_times
                for instant in (left.opened_at, left.closed_at, right.opened_at, right.closed_at)
            )
            if overlap and boundaries_observed:
                return True
    return False


def _evaluate(
    episode: FuturesPositionEpisode,
    summary: VenuePositionSummaryRecord,
    terms: InstrumentDescriptor,
) -> _Evaluation:
    assert episode.max_open_quantity is not None
    assert episode.gross_realized_pnl is not None
    primary = (
        _delta(
            "maxOpenInterest",
            episode.max_open_quantity,
            summary.max_open_interest,
            terms.quantity_step,
        ),
        _delta("closedVolume", episode.closing_volume, summary.closed_volume, terms.quantity_step),
        _money_delta(
            "closingPnl",
            episode.gross_realized_pnl.amount,
            summary.closing_pnl,
            terms.settlement_precision,
        ),
    )
    entry_within, entry_exact = _rational_within(
        episode.average_entry_price,
        episode.entry_price_source_scale,
        summary.entry_price,
        terms.price_tick,
    )
    exit_within, exit_exact = _rational_within(
        episode.average_close_price,
        episode.exit_price_source_scale,
        summary.average_close_price,
        terms.price_tick,
    )
    return _Evaluation(
        deltas=primary,
        primary_within=all(item.absolute_delta.value <= item.tolerance.value for item in primary),
        all_primary_exact=all(item.absolute_delta.value == 0 for item in primary),
        secondary_within=entry_within and exit_within,
        all_secondary_exact=entry_exact and exit_exact,
    )


def _delta(
    field: str,
    actual: ScaledDecimal,
    expected: ScaledDecimal,
    quantum: ScaledDecimal,
) -> ReconciliationDelta:
    tolerance_value = max(source_bound(actual, expected), quantum.value / 2)
    difference = abs(actual.value - expected.value)
    return ReconciliationDelta(
        field=field,
        actual=actual,
        expected=expected,
        absolute_delta=ScaledDecimal.from_decimal(difference),
        tolerance=ScaledDecimal.from_decimal(tolerance_value),
    )


def _money_delta(
    field: str,
    actual: ScaledDecimal,
    expected: ScaledDecimal,
    settlement_precision: int,
) -> ReconciliationDelta:
    tolerance_value = max(
        source_bound(actual, expected), Decimal(1).scaleb(-settlement_precision) / 2
    )
    difference = abs(actual.value - expected.value)
    return ReconciliationDelta(
        field=field,
        actual=actual,
        expected=expected,
        absolute_delta=ScaledDecimal.from_decimal(difference),
        tolerance=ScaledDecimal.from_decimal(tolerance_value),
    )


def _rational_within(
    actual: ExactRational | None,
    actual_scale: int,
    expected: ScaledDecimal,
    tick: ScaledDecimal,
) -> tuple[bool, bool]:
    if actual is None:
        return False, False
    numerator = int(actual.numerator)
    denominator = int(actual.denominator)
    actual_fraction = Fraction(numerator, denominator)
    expected_fraction = Fraction(expected.value)
    difference = abs(actual_fraction - expected_fraction)
    bound = max(
        Decimal(1).scaleb(-actual_scale) / 2 + Decimal(1).scaleb(-expected.scale) / 2,
        tick.value / 2,
    )
    return difference <= Fraction(bound), difference == 0


def _structurally_equal(
    episode: FuturesPositionEpisode,
    summary: VenuePositionSummaryRecord,
) -> bool:
    return (
        episode.instrument_id == summary.instrument_id
        and episode.source_symbol == summary.source_symbol
        and episode.direction is summary.direction
        and episode.opened_at == summary.opened_at
        and episode.closed_at == summary.closed_at
    )


def _components(
    edges: set[tuple[str, str]],
) -> tuple[tuple[set[str], set[str]], ...]:
    remaining: set[tuple[str, str]] = set(edges)
    output: list[tuple[set[str], set[str]]] = []
    while remaining:
        seed = next(iter(remaining))
        episodes = {seed[0]}
        summaries = {seed[1]}
        changed = True
        while changed:
            changed = False
            for episode_id, summary_id in tuple(remaining):
                if episode_id in episodes or summary_id in summaries:
                    before = (len(episodes), len(summaries))
                    episodes.add(episode_id)
                    summaries.add(summary_id)
                    changed = changed or before != (len(episodes), len(summaries))
        remaining = {
            pair for pair in remaining if pair[0] not in episodes and pair[1] not in summaries
        }
        output.append((episodes, summaries))
    return tuple(output)


def _perfect_matchings(
    episode_ids: set[str],
    summary_ids: set[str],
    edges: set[tuple[str, str]],
) -> list[dict[str, str]]:
    if len(episode_ids) != len(summary_ids):
        return []
    ordered = sorted(episode_ids)
    matches: list[dict[str, str]] = []

    def visit(index: int, assigned: set[str], current: dict[str, str]) -> None:
        if len(matches) >= 2:
            return
        if index == len(ordered):
            if assigned == summary_ids:
                matches.append(dict(current))
            return
        episode_id = ordered[index]
        for summary_id in sorted(
            candidate
            for left, candidate in edges
            if left == episode_id and candidate not in assigned
        ):
            current[episode_id] = summary_id
            visit(index + 1, assigned | {summary_id}, current)
            current.pop(episode_id)

    visit(0, set(), {})
    return matches


def _eligible_episode(
    episode: FuturesPositionEpisode,
    summary_id: str,
    status: ReconciliationStatus,
) -> FuturesPositionEpisode:
    capabilities = set(episode.capability.capabilities)
    capabilities.add(Capability.VENUE_POSITION_SUMMARY)
    if episode.net_trading_pnl is not None:
        capabilities.add(Capability.CLOSED_OUTCOME)
    snapshot = replace(
        episode.capability,
        capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
        fee_adjusted_outcome_available=episode.net_trading_pnl is not None,
        reconciliation_available=True,
    )
    return replace(
        episode,
        reconciliation_status=status,
        capability=snapshot,
        eligible=True,
        exclusion_reasons=(),
        evidence_record_ids=tuple(sorted({*episode.evidence_record_ids, summary_id})),
    )


def _excluded_episode(
    episode: FuturesPositionEpisode,
    status: ReconciliationStatus,
    reason: str | None,
) -> FuturesPositionEpisode:
    reasons = set(episode.exclusion_reasons)
    if reason is not None:
        reasons.add(reason)
    return replace(
        episode,
        reconciliation_status=status,
        eligible=False,
        exclusion_reasons=tuple(sorted(reasons)),
    )


def _summary_shell(
    summary: VenuePositionSummaryRecord,
    status: ReconstructionStatus,
    reason: str,
) -> FuturesPositionEpisode:
    capabilities = (
        Capability.POSITION_MODE_USER_DECLARED,
        Capability.VENUE_POSITION_SUMMARY,
    )
    snapshot = CapabilitySnapshot(
        capabilities=capabilities,
        gross_pnl_available=False,
        fee_adjusted_outcome_available=False,
        holding_duration_available=False,
        stable_episode_time_available=False,
        reconciliation_available=False,
        funding_inclusive_outcome_available=False,
        unsupported_fee_assets=(),
    )
    return FuturesPositionEpisode(
        episode_id=stable_id("episode_shell", summary.record_id),
        instrument_id=summary.instrument_id,
        source_symbol=summary.source_symbol,
        direction=summary.direction,
        opened_at=None,
        closed_at=None,
        initial_quantity=None,
        max_open_quantity=None,
        opening_volume=ScaledDecimal.zero(),
        closing_volume=ScaledDecimal.zero(),
        average_entry_price=None,
        average_close_price=None,
        gross_realized_pnl=None,
        fees_by_asset=(),
        net_trading_pnl=None,
        reconstruction_status=status,
        reconciliation_status=ReconciliationStatus.INCOMPLETE_TRADES,
        allocations=(),
        capability=snapshot,
        eligible=False,
        exclusion_reasons=(reason,),
        evidence_record_ids=(summary.record_id,),
    )


def _summary_match(
    summary: VenuePositionSummaryRecord,
    matched: dict[str, str],
    ambiguous_summaries: set[str],
    incomplete_links: dict[str, str],
    edges: set[tuple[str, str]],
    evaluations: dict[tuple[str, str], _Evaluation],
    secondary_used_pairs: set[tuple[str, str]],
) -> ReconciliationMatch:
    episode_id = next(
        (episode for episode, summary_id in matched.items() if summary_id == summary.record_id),
        None,
    )
    if episode_id is None:
        episode_id = next(
            (
                episode
                for episode, summary_id in incomplete_links.items()
                if summary_id == summary.record_id
            ),
            None,
        )
    candidates = tuple(sorted(pair[0] for pair in edges if pair[1] == summary.record_id))
    evaluated_pairs = tuple(
        sorted(
            (pair for pair in evaluations if pair[1] == summary.record_id),
            key=lambda pair: pair[0],
        )
    )
    if summary.source_status != "Closed":
        status = ReconciliationStatus.SUMMARY_MISMATCH
    elif summary.record_id in ambiguous_summaries:
        status = ReconciliationStatus.AMBIGUOUS
    elif summary.record_id in incomplete_links.values():
        status = ReconciliationStatus.INCOMPLETE_TRADES
    elif episode_id is None:
        status = (
            ReconciliationStatus.SUMMARY_MISMATCH
            if evaluated_pairs
            else ReconciliationStatus.INCOMPLETE_TRADES
        )
    else:
        evaluation = evaluations[(episode_id, summary.record_id)]
        status = (
            ReconciliationStatus.EXACT
            if evaluation.all_primary_exact
            and (
                (episode_id, summary.record_id) not in secondary_used_pairs
                or evaluation.all_secondary_exact
            )
            else ReconciliationStatus.WITHIN_ROUNDING_TOLERANCE
        )
    if episode_id is not None and (episode_id, summary.record_id) in evaluations:
        deltas = evaluations[(episode_id, summary.record_id)].deltas
    elif len(evaluated_pairs) == 1:
        deltas = evaluations[evaluated_pairs[0]].deltas
    else:
        deltas = ()
    evidence = (summary.record_id,) if episode_id is None else (summary.record_id, episode_id)
    return ReconciliationMatch(
        summary_record_id=summary.record_id,
        episode_id=episode_id,
        status=status,
        candidate_episode_ids=candidates,
        deltas=deltas,
        evidence_record_ids=evidence,
    )


def _incomplete_summary_links(
    episodes: tuple[FuturesPositionEpisode, ...],
    summaries: dict[str, VenuePositionSummaryRecord],
) -> dict[str, str]:
    candidates: dict[str, list[str]] = {}
    for episode in episodes:
        if episode.reconstruction_status is ReconstructionStatus.COMPLETE:
            continue
        if episode.opened_at is None:
            continue
        for summary in summaries.values():
            if (
                episode.instrument_id == summary.instrument_id
                and episode.source_symbol == summary.source_symbol
                and episode.direction is summary.direction
                and episode.opened_at == summary.opened_at
            ):
                candidates.setdefault(episode.episode_id, []).append(summary.record_id)
    return {
        episode_id: values[0]
        for episode_id, values in candidates.items()
        if len(values) == 1 and sum(values[0] in other for other in candidates.values()) == 1
    }


def _issue(
    code: str,
    episode: FuturesPositionEpisode,
    summary_id: str | None,
) -> ReconciliationIssue:
    evidence = set(episode.evidence_record_ids)
    if summary_id is not None:
        evidence.add(summary_id)
    return ReconciliationIssue(
        code=code,
        severity="WARNING",
        episode_id=episode.episode_id,
        summary_record_id=summary_id,
        evidence_record_ids=tuple(sorted(evidence)),
    )


def _pnl_outside(evaluation: _Evaluation) -> bool:
    return any(
        item.field == "closingPnl" and item.absolute_delta.value > item.tolerance.value
        for item in evaluation.deltas
    )
