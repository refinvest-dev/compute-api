"""ADR-060 behavior metrics over pinned, normalized Trading Review objects."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

from refinvest.trading_review.analytics.model import (
    AnalysisConfig,
    Distribution,
    EvidenceReference,
    ExposurePair,
    ExposureValues,
    FindingEligibility,
    HoldingValues,
    LossEntryRelation,
    Membership,
    MetricResult,
    MetricStatus,
    Outcome,
    OutcomeClassification,
    ReentryValues,
    RelationCandidate,
    utc,
)
from refinvest.trading_review.decimal import ExactRational
from refinvest.trading_review.hashing import timestamp_text
from refinvest.trading_review.model import (
    AllocationAction,
    CanonicalRecord,
    Capability,
    ExecutionRecord,
    FuturesPositionEpisode,
    InstrumentDescriptor,
    ReconciliationStatus,
    ReconstructionStatus,
)

REENTRY = "ENTRY_WITHIN_WINDOW_AFTER_LOSS"
EXPOSURE = "INITIAL_EXPOSURE_CHANGE_AFTER_LOSS"
HOLDING = "WIN_LOSS_HOLDING_DURATION_DIFFERENCE"
WINDOW = timedelta(minutes=30)
OUTCOME_CAPS = (Capability.CLOSED_OUTCOME, Capability.FEES, Capability.STABLE_EPISODE_TIME)


def _rational(value: Fraction | Decimal | int) -> ExactRational:
    fraction = Fraction(value)
    return ExactRational(str(fraction.numerator), str(fraction.denominator))


def _fraction(value: ExactRational) -> Fraction:
    return Fraction(int(value.numerator), int(value.denominator))


def median(values: Iterable[Fraction | Decimal | int]) -> ExactRational:
    ordered = sorted(Fraction(value) for value in values)
    if not ordered:
        raise ValueError("median requires at least one value")
    middle = len(ordered) // 2
    return _rational(
        ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    )


def distribution(values: Iterable[Fraction | Decimal | int]) -> Distribution:
    ordered = sorted(Fraction(value) for value in values)
    if not ordered:
        return Distribution(0, None, None, None, None, None, None)
    size = len(ordered)
    return Distribution(
        count=size,
        minimum=_rational(ordered[0]),
        p25=_rational(ordered[(size - 1) // 4]),  # ceil(.25*n)-1
        median=median(ordered),
        p75=_rational(ordered[(3 * size - 1) // 4]),  # ceil(.75*n)-1
        maximum=_rational(ordered[-1]),
        mean=_rational(sum(ordered, Fraction()) / size),
    )


def _microseconds(delta: timedelta) -> int:
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _exact_product(*values: Decimal) -> Decimal:
    # Decimal multiplication otherwise rounds under the caller's global context.
    digits = sum(len(value.as_tuple().digits) for value in values) + 2
    with localcontext() as context:
        context.prec = max(digits, 28)
        result = Decimal(1)
        for value in values:
            result *= value
        return result


def _exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(len(left.as_tuple().digits), len(right.as_tuple().digits)) + 2
        return left - right


def classify_outcome(episode: FuturesPositionEpisode) -> Outcome:
    caps = set(episode.capability.capabilities)
    net = episode.net_trading_pnl
    if net is None or Capability.CLOSED_OUTCOME not in caps or Capability.FEES not in caps:
        return Outcome.UNAVAILABLE
    if net.amount.value < 0:
        return Outcome.LOSS
    if net.amount.value > 0:
        return Outcome.WIN
    return Outcome.BREAKEVEN


def _quality_reasons(episode: FuturesPositionEpisode) -> tuple[str, ...]:
    reasons: list[str] = []
    if episode.reconstruction_status is not ReconstructionStatus.COMPLETE:
        reasons.append("RECONSTRUCTION_NOT_COMPLETE")
    if episode.reconciliation_status not in (
        ReconciliationStatus.EXACT,
        ReconciliationStatus.WITHIN_ROUNDING_TOLERANCE,
    ):
        reasons.append("RECONCILIATION_NOT_ELIGIBLE")
    return tuple(reasons)


def _time_reasons(episode: FuturesPositionEpisode) -> tuple[str, ...]:
    if (
        Capability.STABLE_EPISODE_TIME not in episode.capability.capabilities
        or not episode.capability.stable_episode_time_available
        or episode.opened_at is None
        or episode.closed_at is None
        or episode.closed_at < episode.opened_at
    ):
        return ("MISSING_STABLE_EPISODE_TIME",)
    return ()


def _outcome_reasons(episode: FuturesPositionEpisode) -> tuple[str, ...]:
    if classify_outcome(episode) is not Outcome.UNAVAILABLE:
        return ()
    reasons = ["OUTCOME_UNAVAILABLE"]
    if Capability.FEES not in episode.capability.capabilities:
        reasons.append("MISSING_FEES")
    return tuple(reasons)


def _evidence(
    episode: FuturesPositionEpisode, records: dict[str, CanonicalRecord]
) -> EvidenceReference:
    allocation_ids = tuple(
        sorted(
            {
                record_id
                for allocation in episode.allocations
                for record_id in allocation.evidence_record_ids
            }
        )
    )
    record_ids = tuple(sorted(set(episode.evidence_record_ids) | set(allocation_ids)))
    missing = set(record_ids) - records.keys()
    if missing:
        raise ValueError(f"canonical evidence missing for episode {episode.episode_id}")
    rows = tuple(
        sorted(
            {
                (row.artifact_id, row.source_row_number, row.source_row_hash)
                for record_id in record_ids
                for row in records[record_id].provenance
            }
        )
    )
    return EvidenceReference(episode.episode_id, allocation_ids, record_ids, rows)


def classify_episode(
    episode: FuturesPositionEpisode, canonical_records: tuple[CanonicalRecord, ...]
) -> OutcomeClassification:
    """Return the exact normalized outcome with its allocation and source links."""
    return _classification(episode, {item.record_id: item for item in canonical_records})


def _classification(
    episode: FuturesPositionEpisode, records: dict[str, CanonicalRecord]
) -> OutcomeClassification:
    net = episode.net_trading_pnl
    return OutcomeClassification(
        episode.episode_id,
        classify_outcome(episode),
        net.amount.value if net is not None else None,
        net.asset if net is not None else None,
        tuple(
            (
                item.execution_record_id,
                item.allocated_reported_pnl.amount.value,
                item.allocated_reported_pnl.asset,
            )
            for item in episode.allocations
        ),
        tuple(
            (item.execution_record_id, item.allocated_fee.amount.value, item.allocated_fee.asset)
            for item in episode.allocations
        ),
        _evidence(episode, records),
    )


def _classifications(
    episodes: Iterable[FuturesPositionEpisode], records: dict[str, CanonicalRecord]
) -> tuple[OutcomeClassification, ...]:
    return tuple(_classification(item, records) for item in episodes)


def _member(
    episode: FuturesPositionEpisode,
    role: str,
    reasons: Iterable[str],
    records: dict[str, CanonicalRecord],
) -> Membership:
    return Membership(
        episode.episode_id, role, tuple(sorted(set(reasons))), _evidence(episode, records)
    )


def _reasons_count(members: Iterable[Membership]) -> tuple[tuple[str, int], ...]:
    distinct = {(member.episode_id, reason) for member in members for reason in member.reasons}
    return tuple(sorted(Counter(reason for _, reason in distinct).items()))


def _evidence_set(*groups: Iterable[Membership]) -> tuple[EvidenceReference, ...]:
    return tuple(
        sorted(
            {member.evidence for group in groups for member in group},
            key=lambda evidence: evidence.episode_id,
        )
    )


def _finding(status: MetricStatus, enough: bool) -> FindingEligibility:
    if status is not MetricStatus.AVAILABLE:
        return FindingEligibility.INELIGIBLE_METRIC_STATUS
    return FindingEligibility.ELIGIBLE if enough else FindingEligibility.INELIGIBLE_SAMPLE


def _initial_notional(
    episode: FuturesPositionEpisode,
    executions: dict[str, ExecutionRecord],
    instruments: dict[str, InstrumentDescriptor],
) -> tuple[Decimal, str] | None:
    if (
        Capability.INITIAL_EXPOSURE not in episode.capability.capabilities
        or episode.initial_quantity is None
    ):
        return None
    opening = next(
        (
            item
            for item in episode.allocations
            if item.action in (AllocationAction.OPEN, AllocationAction.REVERSAL_OPEN)
        ),
        None,
    )
    terms = instruments.get(episode.instrument_id)
    execution = executions.get(opening.execution_record_id) if opening else None
    if terms is None or execution is None:
        return None
    return (
        _exact_product(
            episode.initial_quantity.value, execution.price.value, terms.contract_multiplier.value
        ),
        terms.settlement_asset,
    )


def _notional_inputs(
    episode: FuturesPositionEpisode,
    executions: dict[str, ExecutionRecord],
    instruments: dict[str, InstrumentDescriptor],
) -> tuple[tuple[str, str, str], ...]:
    opening = next(
        (
            item
            for item in episode.allocations
            if item.action in (AllocationAction.OPEN, AllocationAction.REVERSAL_OPEN)
        ),
        None,
    )
    if opening is None or episode.initial_quantity is None:
        return ()
    execution = executions.get(opening.execution_record_id)
    terms = instruments.get(episode.instrument_id)
    if execution is None or terms is None:
        return ()
    return (
        (episode.episode_id, "initialQuantity", episode.initial_quantity.source_lexeme),
        (episode.episode_id, "initialPrice", execution.price.source_lexeme),
        (episode.episode_id, "contractMultiplier", terms.contract_multiplier.source_lexeme),
        (episode.episode_id, "openingRecordId", execution.record_id),
    )


def _loss_key(episode: FuturesPositionEpisode) -> tuple[datetime, str, str]:
    assert episode.closed_at is not None
    return (utc(episode.closed_at), episode.instrument_id, episode.episode_id)


def _entry_key(episode: FuturesPositionEpisode) -> tuple[str, str]:
    return (episode.instrument_id, episode.episode_id)


def _holding_key(episode: FuturesPositionEpisode) -> tuple[int, datetime, str, str]:
    assert episode.opened_at is not None and episode.closed_at is not None
    return (
        _microseconds(episode.closed_at - episode.opened_at),
        utc(episode.closed_at),
        episode.instrument_id,
        episode.episode_id,
    )


def _net_text(episode: FuturesPositionEpisode) -> str:
    if episode.net_trading_pnl is None:
        raise ValueError("holding group requires net outcome")
    return format(episode.net_trading_pnl.amount.value, "f")


def _build_reentry(
    episodes: tuple[FuturesPositionEpisode, ...],
    config: AnalysisConfig,
    records: dict[str, CanonicalRecord],
) -> MetricResult:
    period = config.analysis_period
    losses = sorted((item for item in episodes if period.contains(item.closed_at)), key=_loss_key)
    entries = sorted(
        (item for item in episodes if period.contains(item.opened_at)),
        key=lambda item: (utc(item.opened_at), *_entry_key(item)),
    )  # type: ignore[arg-type]
    subjects: list[Membership] = []
    comparisons: list[Membership] = []
    excluded: list[Membership] = []
    eligible_losses: list[FuturesPositionEpisode] = []
    missing_capability = False
    for loss in losses:
        reasons = list(_quality_reasons(loss))
        if not reasons:
            outcome = classify_outcome(loss)
            if outcome is Outcome.UNAVAILABLE:
                reasons.extend(_outcome_reasons(loss))
                missing_capability = True
            elif outcome is not Outcome.LOSS:
                reasons.append("OUTCOME_NOT_LOSS")
            if outcome in (Outcome.LOSS, Outcome.UNAVAILABLE):
                time_reasons = _time_reasons(loss)
                reasons.extend(time_reasons)
                missing_capability |= bool(time_reasons)
            if (
                not reasons
                and loss.closed_at is not None
                and utc(loss.closed_at) + WINDOW > period.end_exclusive
            ):
                reasons.append("WINDOW_RIGHT_CENSORED")
        if reasons:
            excluded.append(_member(loss, "EXCLUDED_CANDIDATE", reasons, records))
        else:
            eligible_losses.append(loss)
            subjects.append(_member(loss, "SUPPORTING", (), records))

    relations: list[LossEntryRelation] = []
    linked_ids: set[str] = set()
    audited: dict[str, set[str]] = defaultdict(set)
    elapsed: list[int] = []
    same_losses = different_losses = same_members = different_members = 0
    for loss in eligible_losses:
        assert loss.closed_at is not None
        cutoff = utc(loss.closed_at) + WINDOW
        qualified: list[FuturesPositionEpisode] = []
        candidate_roles: list[tuple[FuturesPositionEpisode, str]] = []
        for entry in entries:
            if entry.episode_id == loss.episode_id or entry.opened_at is None:
                continue
            instant = utc(entry.opened_at)
            quality = _quality_reasons(entry)
            opening = entry.allocations[0].action if entry.allocations else None
            if quality:
                continue
            if opening is AllocationAction.REVERSAL_OPEN:
                if utc(loss.closed_at) <= instant <= cutoff:
                    candidate_roles.append((entry, "REVERSAL_SEPARATE"))
                    audited[entry.episode_id].add("ENTRY_IS_REVERSAL")
            elif instant == utc(loss.closed_at):
                candidate_roles.append((entry, "SAME_TIMESTAMP"))
                audited[entry.episode_id].add("ENTRY_AT_SAME_TIMESTAMP")
            elif instant > cutoff:
                candidate_roles.append((entry, "AFTER_WINDOW"))
                audited[entry.episode_id].add("ENTRY_AFTER_WINDOW")
            elif instant > utc(loss.closed_at):
                time_reasons = _time_reasons(entry)
                if time_reasons:
                    missing_capability = True
                    audited[entry.episode_id].update(time_reasons)
                    candidate_roles.append((entry, "MISSING_STABLE_EPISODE_TIME"))
                elif opening is AllocationAction.OPEN:
                    qualified.append(entry)
        earliest = min(
            (utc(entry.opened_at) for entry in qualified if entry.opened_at), default=None
        )
        related = sorted(
            (entry for entry in qualified if utc(entry.opened_at) == earliest), key=_entry_key
        )
        for entry in qualified:
            if entry in related:
                candidate_roles.append((entry, "SELECTED_EARLIEST"))
                linked_ids.add(entry.episode_id)
            else:
                candidate_roles.append((entry, "LATER_NOT_LINKED"))
                audited[entry.episode_id].add("LATER_NOT_LINKED")
        if earliest is not None:
            elapsed.append(_microseconds(earliest - utc(loss.closed_at)))
            same = sum(entry.instrument_id == loss.instrument_id for entry in related)
            different = len(related) - same
            same_losses += bool(same)
            different_losses += bool(different)
            same_members += same
            different_members += different
        candidates = tuple(
            RelationCandidate(entry.episode_id, role, entry.opened_at, _evidence(entry, records))
            for entry, role in sorted(
                candidate_roles, key=lambda pair: (utc(pair[0].opened_at), *_entry_key(pair[0]))
            )
        )  # type: ignore[arg-type]
        relations.append(
            LossEntryRelation(
                loss.episode_id,
                loss.closed_at,
                candidates,
                tuple(entry.episode_id for entry in related),
                elapsed[-1] if earliest is not None else None,
            )
        )
    for entry in entries:
        if entry.episode_id in linked_ids:
            comparisons.append(_member(entry, "COMPARISON", (), records))
        elif entry.episode_id in audited:
            excluded.append(
                _member(entry, "SELECTION_BOUNDARY", audited[entry.episode_id], records)
            )
    comparisons.sort(
        key=lambda member: _entry_key(
            next(item for item in entries if item.episode_id == member.episode_id)
        )
    )
    excluded.sort(key=lambda member: (member.episode_id, member.role))
    status = (
        MetricStatus.MISSING_CAPABILITY
        if missing_capability
        else MetricStatus.INSUFFICIENT_SAMPLE
        if len(eligible_losses) < config.metric_minimums.reentry_eligible_losses
        else MetricStatus.AVAILABLE
    )
    matched = sum(bool(relation.related_entry_ids) for relation in relations)
    values = ReentryValues(
        len(eligible_losses),
        matched,
        sum(len(relation.related_entry_ids) for relation in relations),
        same_losses,
        different_losses,
        same_members,
        different_members,
        _rational(Fraction(matched, len(eligible_losses)))
        if status is MetricStatus.AVAILABLE
        else None,
        distribution(elapsed) if status is MetricStatus.AVAILABLE else None,
    )
    return MetricResult(
        REENTRY,
        "1",
        config,
        status,
        _finding(status, len(eligible_losses) >= config.finding_minimums.reentry_eligible_losses),
        OUTCOME_CAPS,
        len(losses),
        len(eligible_losses),
        len(losses) - len(eligible_losses),
        _reasons_count(excluded),
        tuple(subjects),
        tuple(comparisons),
        tuple(excluded),
        _evidence_set(subjects, comparisons, excluded),
        tuple(
            sorted(
                {
                    (relation.loss_id, "closedAt", timestamp_text(relation.closed_at))
                    for relation in relations
                }
                | {
                    (candidate.entry_id, "openedAt", timestamp_text(candidate.opened_at))
                    for relation in relations
                    for candidate in relation.candidates
                    if candidate.opened_at is not None
                }
                | {
                    (relation.loss_id, "elapsedMicroseconds", str(relation.elapsed_microseconds))
                    for relation in relations
                    if relation.elapsed_microseconds is not None
                }
            )
        ),
        values,
        tuple(relations),
        _classifications(losses, records),
    )


def _build_exposure(
    episodes: tuple[FuturesPositionEpisode, ...],
    config: AnalysisConfig,
    records: dict[str, CanonicalRecord],
    executions: dict[str, ExecutionRecord],
    instruments: dict[str, InstrumentDescriptor],
    reentry: MetricResult,
) -> MetricResult:
    by_id = {item.episode_id: item for item in episodes}
    pairs: list[ExposurePair] = []
    excluded = list(reentry.excluded_membership)
    subjects: list[Membership] = []
    comparisons: list[Membership] = []
    baseline_members: dict[str, Membership] = {}
    missing = reentry.status is MetricStatus.MISSING_CAPABILITY
    for relation in reentry.relations:
        loss = by_id[relation.loss_id]
        if not relation.related_entry_ids:
            excluded.append(_member(loss, "EXCLUDED_CANDIDATE", ("NO_RELATED_ENTRY",), records))
        for entry_id in relation.related_entry_ids:
            entry = by_id[entry_id]
            loss_value = _initial_notional(loss, executions, instruments)
            entry_value = _initial_notional(entry, executions, instruments)
            if loss_value is None:
                excluded.append(
                    _member(loss, "EXCLUDED_CANDIDATE", ("MISSING_INITIAL_EXPOSURE",), records)
                )
            if entry_value is None:
                excluded.append(
                    _member(entry, "EXCLUDED_CANDIDATE", ("MISSING_INITIAL_EXPOSURE",), records)
                )
            if loss_value is None or entry_value is None:
                missing = True
                continue
            loss_notional, loss_currency = loss_value
            entry_notional, entry_currency = entry_value
            if loss_currency != entry_currency:
                excluded.append(
                    _member(entry, "EXCLUDED_CANDIDATE", ("SETTLEMENT_CURRENCY_MISMATCH",), records)
                )
                continue
            if loss_notional == 0:
                excluded.append(
                    _member(entry, "EXCLUDED_CANDIDATE", ("ZERO_DENOMINATOR",), records)
                )
                continue
            assert loss.closed_at is not None and entry.opened_at is not None
            baseline: list[tuple[FuturesPositionEpisode, Decimal]] = []
            for candidate in episodes:
                if (
                    candidate.episode_id in {loss.episode_id, *relation.related_entry_ids}
                    or candidate.instrument_id != entry.instrument_id
                    or candidate.direction != entry.direction
                    or candidate.opened_at is None
                    or candidate.closed_at is None
                    or not (
                        config.analysis_period.start_inclusive <= utc(candidate.opened_at)
                        and utc(candidate.closed_at) < utc(loss.closed_at)
                    )
                ):
                    continue
                quality = _quality_reasons(candidate)
                if quality:
                    excluded.append(_member(candidate, "EXCLUDED_CANDIDATE", quality, records))
                    continue
                time_reasons = _time_reasons(candidate)
                if time_reasons:
                    excluded.append(_member(candidate, "EXCLUDED_CANDIDATE", time_reasons, records))
                    continue
                value = _initial_notional(candidate, executions, instruments)
                if value is None:
                    excluded.append(
                        _member(
                            candidate, "EXCLUDED_CANDIDATE", ("MISSING_INITIAL_EXPOSURE",), records
                        )
                    )
                elif value[1] != entry_currency:
                    excluded.append(
                        _member(
                            candidate,
                            "EXCLUDED_CANDIDATE",
                            ("SETTLEMENT_CURRENCY_MISMATCH",),
                            records,
                        )
                    )
                else:
                    baseline.append((candidate, value[0]))
            baseline.sort(
                key=lambda pair: (
                    utc(pair[0].closed_at),
                    utc(pair[0].opened_at),
                    pair[0].instrument_id,
                    pair[0].episode_id,
                )
            )  # type: ignore[arg-type]
            for candidate, _ in baseline:
                baseline_members[candidate.episode_id] = _member(
                    candidate, "COMPARISON", (), records
                )
            baseline_median = baseline_ratio = baseline_delta = None
            baseline_reason = None
            if len(baseline) < config.metric_minimums.prior_baseline_episodes:
                baseline_reason = "INSUFFICIENT_PRIOR_BASELINE"
            else:
                baseline_median = median(value for _, value in baseline)
                if _fraction(baseline_median) == 0:
                    baseline_reason = "ZERO_DENOMINATOR"
                else:
                    baseline_ratio = _rational(
                        Fraction(entry_notional) / _fraction(baseline_median)
                    )
                    baseline_delta = _rational(
                        Fraction(entry_notional) - _fraction(baseline_median)
                    )
            ratio = ExactRational.divide(entry_notional, loss_notional)
            pairs.append(
                ExposurePair(
                    loss.episode_id,
                    entry_id,
                    entry_currency,
                    loss_notional,
                    entry_notional,
                    _exact_subtract(entry_notional, loss_notional),
                    ratio,
                    _rational(_fraction(ratio) - 1),
                    loss.instrument_id == entry.instrument_id,
                    loss.direction == entry.direction,
                    tuple(candidate.episode_id for candidate, _ in baseline),
                    baseline_median,
                    baseline_ratio,
                    baseline_delta,
                    baseline_reason,
                )
            )
            subjects.append(_member(loss, "SUPPORTING", (), records))
            comparisons.append(_member(entry, "COMPARISON", (), records))
    status = (
        MetricStatus.MISSING_CAPABILITY
        if missing
        else MetricStatus.INSUFFICIENT_SAMPLE
        if len(pairs) < config.metric_minimums.exposure_pairs
        else MetricStatus.AVAILABLE
    )
    currencies: dict[str, list[Decimal]] = defaultdict(list)
    for pair in pairs:
        currencies[pair.currency].append(pair.absolute_delta)
    show = status is MetricStatus.AVAILABLE
    values = ExposureValues(
        len(pairs),
        sum(pair.absolute_delta > 0 for pair in pairs),
        sum(pair.absolute_delta == 0 for pair in pairs),
        sum(pair.absolute_delta < 0 for pair in pairs),
        median(_fraction(pair.ratio) for pair in pairs) if show else None,
        distribution(_fraction(pair.ratio) for pair in pairs) if show else None,
        distribution(_fraction(pair.percentage_delta) for pair in pairs) if show else None,
        tuple((currency, distribution(amounts)) for currency, amounts in sorted(currencies.items()))
        if show
        else (),
        tuple(pairs),
    )
    subjects = list({member.episode_id: member for member in subjects}.values())
    comparisons = list({member.episode_id: member for member in comparisons}.values())
    baseline_order = sorted(
        baseline_members,
        key=lambda episode_id: (
            utc(by_id[episode_id].closed_at),
            utc(by_id[episode_id].opened_at),
            by_id[episode_id].instrument_id,
            episode_id,
        ),
    )
    comparison = tuple(comparisons) + tuple(baseline_members[key] for key in baseline_order)
    excluded.sort(key=lambda member: (member.episode_id, member.role, member.reasons))
    return MetricResult(
        EXPOSURE,
        "1",
        config,
        status,
        _finding(status, len(pairs) >= config.finding_minimums.exposure_pairs),
        (*OUTCOME_CAPS, Capability.INITIAL_EXPOSURE),
        reentry.population_count,
        len({pair.loss_id for pair in pairs}),
        reentry.population_count - len({pair.loss_id for pair in pairs}),
        _reasons_count(excluded),
        tuple(subjects),
        comparison,
        tuple(excluded),
        _evidence_set(subjects, comparison, excluded),
        tuple(
            sorted(
                {
                    scalar
                    for pair in pairs
                    for scalar in (
                        *_notional_inputs(by_id[pair.loss_id], executions, instruments),
                        *_notional_inputs(by_id[pair.entry_id], executions, instruments),
                        (pair.loss_id, "initialNotional", format(pair.loss_notional, "f")),
                        (pair.entry_id, "initialNotional", format(pair.entry_notional, "f")),
                        (pair.entry_id, "settlementCurrency", pair.currency),
                        (
                            pair.entry_id,
                            "ratio",
                            f"{pair.ratio.numerator}/{pair.ratio.denominator}",
                        ),
                    )
                }
                | {
                    scalar
                    for pair in pairs
                    for baseline_id in pair.baseline_episode_ids
                    for scalar in _notional_inputs(by_id[baseline_id], executions, instruments)
                }
            )
        ),
        values,
        reentry.relations,
        reentry.outcome_classifications,
    )


def _build_holding(
    episodes: tuple[FuturesPositionEpisode, ...],
    config: AnalysisConfig,
    records: dict[str, CanonicalRecord],
) -> MetricResult:
    population = sorted(
        (item for item in episodes if config.analysis_period.contains(item.closed_at)),
        key=_loss_key,
    )
    wins: list[FuturesPositionEpisode] = []
    losses: list[FuturesPositionEpisode] = []
    excluded: list[Membership] = []
    breakeven = unavailable = 0
    missing = False
    for episode in population:
        reasons = list(_quality_reasons(episode))
        if not reasons:
            reasons.extend(_time_reasons(episode))
            outcome = classify_outcome(episode)
            if outcome is Outcome.UNAVAILABLE:
                reasons.extend(_outcome_reasons(episode))
                unavailable += 1
            elif outcome is Outcome.BREAKEVEN:
                reasons.append("OUTCOME_BREAKEVEN")
                breakeven += 1
            elif not reasons:
                (wins if outcome is Outcome.WIN else losses).append(episode)
            missing |= "MISSING_STABLE_EPISODE_TIME" in reasons or outcome is Outcome.UNAVAILABLE
        if reasons:
            excluded.append(_member(episode, "EXCLUDED_CANDIDATE", reasons, records))
    wins.sort(key=_holding_key)
    losses.sort(key=_holding_key)
    status = (
        MetricStatus.MISSING_CAPABILITY
        if missing
        else MetricStatus.INSUFFICIENT_SAMPLE
        if (
            len(wins) < config.metric_minimums.holding_wins
            or len(losses) < config.metric_minimums.holding_losses
        )
        else MetricStatus.AVAILABLE
    )
    show = status is MetricStatus.AVAILABLE
    win_durations = [_holding_key(item)[0] for item in wins]
    loss_durations = [_holding_key(item)[0] for item in losses]
    win_dist = distribution(win_durations) if show else None
    loss_dist = distribution(loss_durations) if show else None
    difference = ratio = None
    ratio_reason = None
    if show:
        assert win_dist is not None and loss_dist is not None
        assert win_dist.median is not None and loss_dist.median is not None
        difference = _rational(_fraction(loss_dist.median) - _fraction(win_dist.median))
        if _fraction(win_dist.median) == 0:
            ratio_reason = "ZERO_DENOMINATOR"
        else:
            ratio = _rational(_fraction(loss_dist.median) / _fraction(win_dist.median))
    subjects = tuple(_member(item, "SUPPORTING", (), records) for item in wins)
    comparisons = tuple(_member(item, "COMPARISON", (), records) for item in losses)
    values = HoldingValues(
        len(wins),
        len(losses),
        breakeven,
        unavailable,
        win_dist,
        loss_dist,
        difference,
        ratio,
        ratio_reason,
    )
    return MetricResult(
        HOLDING,
        "1",
        config,
        status,
        _finding(
            status,
            len(wins) >= config.finding_minimums.holding_wins
            and len(losses) >= config.finding_minimums.holding_losses,
        ),
        OUTCOME_CAPS,
        len(population),
        len(wins) + len(losses),
        len(population) - len(wins) - len(losses),
        _reasons_count(excluded),
        subjects,
        comparisons,
        tuple(excluded),
        _evidence_set(subjects, comparisons, excluded),
        tuple(
            sorted(
                {
                    scalar
                    for item in (*wins, *losses)
                    for scalar in (
                        (item.episode_id, "openedAt", timestamp_text(utc(item.opened_at))),
                        (item.episode_id, "closedAt", timestamp_text(utc(item.closed_at))),
                        (
                            item.episode_id,
                            "holdingDurationMicroseconds",
                            str(_holding_key(item)[0]),
                        ),
                        (
                            item.episode_id,
                            "netTradingPnl",
                            _net_text(item),
                        ),
                    )
                }
            )
        ),
        values,
        (),
        _classifications(population, records),
    )


def calculate_behavior_metrics(
    *,
    episodes: tuple[FuturesPositionEpisode, ...],
    canonical_records: tuple[CanonicalRecord, ...],
    instruments: tuple[InstrumentDescriptor, ...],
    analysis_config: AnalysisConfig,
) -> tuple[MetricResult, MetricResult, MetricResult]:
    """Calculate all three definition v1 metrics without I/O or input mutation."""
    if len({item.episode_id for item in episodes}) != len(episodes):
        raise ValueError("episode IDs must be unique")
    records = {item.record_id: item for item in canonical_records}
    if len(records) != len(canonical_records):
        raise ValueError("canonical record IDs must be unique")
    terms = {item.instrument_id: item for item in instruments}
    if len(terms) != len(instruments):
        raise ValueError("instrument IDs must be unique")
    if any(item.instrument_id not in terms for item in episodes):
        raise ValueError("pinned instrument terms missing for episode")
    if any(
        item.net_trading_pnl is not None
        and item.net_trading_pnl.asset != terms[item.instrument_id].settlement_asset
        for item in episodes
    ):
        raise ValueError("net trading PnL must use pinned settlement currency")
    executions = {key: item for key, item in records.items() if isinstance(item, ExecutionRecord)}
    reentry = _build_reentry(episodes, analysis_config, records)
    exposure = _build_exposure(episodes, analysis_config, records, executions, terms, reentry)
    holding = _build_holding(episodes, analysis_config, records)
    return reentry, exposure, holding
