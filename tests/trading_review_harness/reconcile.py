from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
from decimal import Decimal
from typing import Any

from tests.trading_review_harness.canonical import content_hash
from tests.trading_review_harness.model import Episode, InstrumentTerms, PositionSummary


def _ulp(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    return Decimal(1).scaleb(exponent if exponent < 0 else 0)


def source_bound(left: Decimal, right: Decimal) -> Decimal:
    return _ulp(left) / 2 + _ulp(right) / 2


def quantity_tolerance(left: Decimal, right: Decimal, terms: InstrumentTerms) -> Decimal:
    return max(source_bound(left, right), terms.step_size / 2)


def price_tolerance(left: Decimal, right: Decimal, terms: InstrumentTerms) -> Decimal:
    return max(source_bound(left, right), terms.tick_size / 2)


def money_tolerance(left: Decimal, right: Decimal, terms: InstrumentTerms) -> Decimal:
    settlement_quantum = Decimal(1).scaleb(-terms.settlement_precision)
    return max(source_bound(left, right), settlement_quantum / 2)


def _rational_equal_within(
    numerator: Decimal,
    denominator: Decimal,
    source: Decimal,
    tolerance: Decimal,
) -> tuple[bool, bool, Decimal, Decimal]:
    numerator_delta = abs(numerator - source * denominator)
    return (
        numerator_delta <= tolerance * denominator,
        numerator_delta == 0,
        numerator_delta,
        denominator,
    )


def _comparison(
    episode: Episode,
    summary: PositionSummary,
    terms: InstrumentTerms,
    include_prices: bool,
) -> dict[str, Any]:
    max_quantity = episode.max_quantity or Decimal(0)
    gross_pnl = episode.gross_pnl or Decimal(0)
    deltas = {
        "maxQuantity": abs(max_quantity - summary.max_quantity),
        "closedVolume": abs(episode.closing_volume - summary.closed_volume),
        "grossTradingPnl": abs(gross_pnl - summary.closing_pnl),
    }
    tolerances = {
        "maxQuantity": quantity_tolerance(max_quantity, summary.max_quantity, terms),
        "closedVolume": quantity_tolerance(episode.closing_volume, summary.closed_volume, terms),
        "grossTradingPnl": money_tolerance(gross_pnl, summary.closing_pnl, terms),
    }
    exact = all(value == 0 for value in deltas.values())
    within = all(deltas[name] <= tolerances[name] for name in deltas)
    price_data: dict[str, dict[str, Decimal | bool]] = {}
    if include_prices:
        if episode.entry_numerator is None or episode.entry_denominator is None:
            within = False
        else:
            tolerance = price_tolerance(
                episode.entry_numerator / episode.entry_denominator,
                summary.entry_price,
                terms,
            )
            accepted, price_exact, numerator_delta, denominator = _rational_equal_within(
                episode.entry_numerator,
                episode.entry_denominator,
                summary.entry_price,
                tolerance,
            )
            price_data["averageEntry"] = {
                "accepted": accepted,
                "exact": price_exact,
                "numeratorDelta": numerator_delta,
                "denominator": denominator,
                "tolerance": tolerance,
            }
            within = within and accepted
            exact = exact and price_exact
        if episode.exit_numerator is None or episode.exit_denominator is None:
            within = False
        else:
            tolerance = price_tolerance(
                episode.exit_numerator / episode.exit_denominator,
                summary.close_price,
                terms,
            )
            accepted, price_exact, numerator_delta, denominator = _rational_equal_within(
                episode.exit_numerator,
                episode.exit_denominator,
                summary.close_price,
                tolerance,
            )
            price_data["averageExit"] = {
                "accepted": accepted,
                "exact": price_exact,
                "numeratorDelta": numerator_delta,
                "denominator": denominator,
                "tolerance": tolerance,
            }
            within = within and accepted
            exact = exact and price_exact
    return {
        "deltas": deltas,
        "tolerances": tolerances,
        "prices": price_data,
        "within": within,
        "exact": exact,
    }


def _temporal_match(episode: Episode, summary: PositionSummary) -> bool:
    if episode.opened_at is None or episode.closed_at is None:
        return False
    minute = timedelta(minutes=1)
    return (
        summary.opened_minute <= episode.opened_at < summary.opened_minute + minute
        and summary.closed_minute <= episode.closed_at < summary.closed_minute + minute
    )


def _perfect_matchings(
    episode_keys: list[str], candidate_map: dict[str, list[str]]
) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []

    def visit(index: int, used: set[str], current: dict[str, str]) -> None:
        if len(matches) > 1:
            return
        if index == len(episode_keys):
            matches.append(dict(current))
            return
        episode_key = episode_keys[index]
        for summary_key in sorted(candidate_map.get(episode_key, [])):
            if summary_key in used:
                continue
            used.add(summary_key)
            current[episode_key] = summary_key
            visit(index + 1, used, current)
            current.pop(episode_key)
            used.remove(summary_key)

    visit(0, set(), {})
    return matches


def _components(edges: set[tuple[str, str]]) -> list[tuple[set[str], set[str]]]:
    by_episode: dict[str, set[str]] = defaultdict(set)
    by_summary: dict[str, set[str]] = defaultdict(set)
    for episode_key, summary_key in edges:
        by_episode[episode_key].add(summary_key)
        by_summary[summary_key].add(episode_key)
    components: list[tuple[set[str], set[str]]] = []
    seen_episodes: set[str] = set()
    for start in sorted(by_episode):
        if start in seen_episodes:
            continue
        episode_keys: set[str] = set()
        summary_keys: set[str] = set()
        queue: deque[tuple[str, str]] = deque([("episode", start)])
        while queue:
            kind, key = queue.popleft()
            if kind == "episode":
                if key in episode_keys:
                    continue
                episode_keys.add(key)
                seen_episodes.add(key)
                queue.extend(("summary", value) for value in by_episode[key])
            else:
                if key in summary_keys:
                    continue
                summary_keys.add(key)
                queue.extend(("episode", value) for value in by_summary[key])
        components.append((episode_keys, summary_keys))
    return components


def _shell_for_summary(summary: PositionSummary, status: str, reason: str) -> Episode:
    return Episode(
        key="shell-" + content_hash({"summaryKey": summary.key})[:20],
        symbol=summary.symbol,
        direction=summary.direction,
        opened_at=None,
        closed_at=None,
        initial_quantity=None,
        max_quantity=None,
        opening_volume=Decimal(0),
        closing_volume=Decimal(0),
        entry_numerator=None,
        entry_denominator=None,
        exit_numerator=None,
        exit_denominator=None,
        gross_pnl=None,
        fees={},
        net_pnl=None,
        reconstruction_status=status,
        reconciliation_status="INCOMPLETE_TRADES",
        capabilities={"POSITION_MODE_USER_DECLARED", "VENUE_POSITION_SUMMARY"},
        eligible=False,
        exclusion_reasons=[reason],
        provenance=[summary.provenance],
    )


def reconcile(
    episodes: list[Episode],
    summaries: list[PositionSummary],
    terms_by_symbol: dict[str, InstrumentTerms],
) -> tuple[list[Episode], list[dict[str, Any]]]:
    minute = timedelta(minutes=1)
    for episode in episodes:
        if episode.reconstruction_status != "RIGHT_CENSORED" or episode.opened_at is None:
            continue
        missing_close_evidence = any(
            summary.status == "Closed"
            and summary.symbol == episode.symbol
            and summary.direction == episode.direction
            and summary.opened_minute <= episode.opened_at < summary.opened_minute + minute
            and summary.closed_volume > episode.closing_volume
            for summary in summaries
        )
        if missing_close_evidence:
            episode.reconstruction_status = "INCONSISTENT"
            episode.exclusion_reasons.append("MISSING_EXECUTION_SUSPECTED")
    complete = {item.key: item for item in episodes if item.reconstruction_status == "COMPLETE"}
    supported = {item.key: item for item in summaries if item.status == "Closed"}
    comparisons: dict[tuple[str, str], dict[str, Any]] = {}
    primary_edges: set[tuple[str, str]] = set()
    structural_pairs: set[tuple[str, str]] = set()
    for episode in complete.values():
        terms = terms_by_symbol[episode.symbol]
        for summary in supported.values():
            if episode.symbol != summary.symbol or episode.direction != summary.direction:
                continue
            if not _temporal_match(episode, summary):
                continue
            structural_pairs.add((episode.key, summary.key))
            comparison = _comparison(episode, summary, terms, include_prices=False)
            comparisons[(episode.key, summary.key)] = comparison
            if bool(comparison["within"]):
                primary_edges.add((episode.key, summary.key))

    final_edges = set(primary_edges)
    for episode_keys, summary_keys in _components(primary_edges):
        edge_count = sum(
            (episode_key, summary_key) in primary_edges
            for episode_key in episode_keys
            for summary_key in summary_keys
        )
        if edge_count <= 1:
            continue
        for edge in list(final_edges):
            if edge[0] not in episode_keys or edge[1] not in summary_keys:
                continue
            comparison = _comparison(
                complete[edge[0]],
                supported[edge[1]],
                terms_by_symbol[complete[edge[0]].symbol],
                include_prices=True,
            )
            comparisons[edge] = comparison
            if not bool(comparison["within"]):
                final_edges.remove(edge)

    matched_summary_to_episode: dict[str, str] = {}
    ambiguous_episodes: set[str] = set()
    ambiguous_summaries: set[str] = set()
    for episode_keys, summary_keys in _components(final_edges):
        candidate_map = {
            episode_key: sorted(
                summary_key
                for candidate_episode, summary_key in final_edges
                if candidate_episode == episode_key
            )
            for episode_key in episode_keys
        }
        matchings = (
            _perfect_matchings(sorted(episode_keys), candidate_map)
            if len(episode_keys) == len(summary_keys)
            else []
        )
        if len(matchings) == 1:
            for episode_key, summary_key in matchings[0].items():
                matched_summary_to_episode[summary_key] = episode_key
        else:
            ambiguous_episodes.update(episode_keys)
            ambiguous_summaries.update(summary_keys)

    for episode in episodes:
        if episode.key in ambiguous_episodes:
            episode.reconciliation_status = "AMBIGUOUS"
            if "AMBIGUOUS_POSITION_SUMMARY" not in episode.exclusion_reasons:
                episode.exclusion_reasons.append("AMBIGUOUS_POSITION_SUMMARY")
            continue
        matching_summary = next(
            (
                summary_key
                for summary_key, episode_key in matched_summary_to_episode.items()
                if episode_key == episode.key
            ),
            None,
        )
        if matching_summary is not None:
            comparison = comparisons[(episode.key, matching_summary)]
            matched_summary = supported[matching_summary]
            if all(
                item.reference() != matched_summary.provenance.reference()
                for item in episode.provenance
            ):
                episode.provenance.append(matched_summary.provenance)
                episode.provenance.sort(key=lambda item: item.reference())
            episode.reconciliation_status = (
                "EXACT" if bool(comparison["exact"]) else "WITHIN_ROUNDING_TOLERANCE"
            )
            episode.capabilities.update(
                {"POSITION_MODE_OBSERVED_COMPATIBLE", "VENUE_POSITION_SUMMARY"}
            )
            episode.eligible = True
            if episode.net_pnl is not None:
                episode.capabilities.add("CLOSED_OUTCOME")
            continue
        if episode.reconstruction_status == "COMPLETE":
            episode.reconciliation_status = "SUMMARY_MISMATCH"
            related = [pair for pair in structural_pairs if pair[0] == episode.key]
            unsupported = any(
                summary.symbol == episode.symbol
                and summary.direction == episode.direction
                and _temporal_match(episode, summary)
                and summary.status != "Closed"
                for summary in summaries
            )
            if unsupported:
                episode.exclusion_reasons.append("UNSUPPORTED_POSITION_STATUS")
            elif any(
                comparisons[pair]["deltas"]["grossTradingPnl"]
                > comparisons[pair]["tolerances"]["grossTradingPnl"]
                for pair in related
            ):
                episode.exclusion_reasons.append("PNL_MISMATCH")
            else:
                episode.exclusion_reasons.append("POSITION_SUMMARY_MISSING")

    matched_summary_keys = set(matched_summary_to_episode)
    for summary in supported.values():
        if summary.key in matched_summary_keys or summary.key in ambiguous_summaries:
            continue
        symbol_episodes = [episode for episode in episodes if episode.symbol == summary.symbol]
        if not symbol_episodes:
            episodes.append(
                _shell_for_summary(summary, "INCONSISTENT", "POSITION_SUMMARY_WITHOUT_TRADES")
            )
        elif all(
            episode.opened_at is None or summary.opened_minute + minute <= episode.opened_at
            for episode in symbol_episodes
        ) and not any(pair[1] == summary.key for pair in structural_pairs):
            episodes.append(_shell_for_summary(summary, "LEFT_CENSORED", "EPISODE_LEFT_CENSORED"))

    summary_results: list[dict[str, Any]] = []
    for summary in summaries:
        candidate_keys = sorted(
            episode_key for episode_key, summary_key in final_edges if summary_key == summary.key
        )
        episode_key = matched_summary_to_episode.get(summary.key)
        if summary.status != "Closed":
            status = "SUMMARY_MISMATCH"
        elif summary.key in ambiguous_summaries:
            status = "AMBIGUOUS"
        elif episode_key is not None:
            status = complete[episode_key].reconciliation_status
        else:
            status = "INCOMPLETE_TRADES"
        evaluated = []
        for pair in sorted(pair for pair in comparisons if pair[1] == summary.key):
            evaluated.append({"episodeKey": pair[0], **comparisons[pair]})
        summary_results.append(
            {
                "summaryKey": summary.key,
                "summarySourceRow": summary.provenance.source_row,
                "episodeKey": episode_key,
                "status": status,
                "candidateEpisodeKeys": candidate_keys,
                "evaluatedPairs": evaluated,
                "provenance": [summary.provenance.reference()],
            }
        )

    for episode in episodes:
        episode.exclusion_reasons = sorted(set(episode.exclusion_reasons))
        episode.capabilities.discard("EXECUTION_ORIGIN_KNOWN")
    episodes.sort(key=lambda item: (item.symbol, item.opened_at is None, item.opened_at, item.key))
    summary_results.sort(key=lambda item: str(item["summaryKey"]))
    return episodes, summary_results
