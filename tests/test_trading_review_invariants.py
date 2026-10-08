from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from refinvest.trading_review import TradingReviewError, normalize_and_reconstruct
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.invariants import verify_conservation
from refinvest.trading_review.model import AllocationAction, ReconciliationStatus
from tests.trading_review_support import simple_long_request


def _records():  # type: ignore[no-untyped-def]
    result = normalize_and_reconstruct(simple_long_request())
    executions = tuple(record for record in result.canonical_records if hasattr(record, "side"))
    fees = tuple(record for record in result.canonical_records if hasattr(record, "fee_kind"))
    pnls = tuple(record for record in result.canonical_records if hasattr(record, "realized_pnl"))
    return result, executions, fees, pnls


def test_domain_model_is_immutable() -> None:
    result = normalize_and_reconstruct(simple_long_request())
    with pytest.raises(FrozenInstanceError):
        result.episodes[0].eligible = False  # type: ignore[misc]


@pytest.mark.parametrize("mutation", ["quantity", "fee", "pnl"])
def test_conservation_mutation_fails_closed(mutation: str) -> None:
    result, executions, fees, pnls = _records()
    episode = result.episodes[0]
    first = episode.allocations[0]
    if mutation == "quantity":
        first = replace(first, allocated_quantity=ScaledDecimal.parse("2.01"))
    elif mutation == "fee":
        first = replace(
            first,
            allocated_fee=replace(
                first.allocated_fee,
                amount=ScaledDecimal.from_decimal(
                    first.allocated_fee.amount.value + Decimal("0.01"), scale=2
                ),
            ),
        )
    else:
        first = replace(
            first,
            allocated_reported_pnl=replace(
                first.allocated_reported_pnl,
                amount=ScaledDecimal.parse("0.01"),
            ),
        )
    mutated = replace(episode, allocations=(first, *episode.allocations[1:]))
    with pytest.raises(TradingReviewError):
        verify_conservation(executions, fees, pnls, (mutated,))


def test_non_reversal_duplicate_allocation_fails_closed() -> None:
    result, executions, fees, pnls = _records()
    episode = result.episodes[0]
    original = episode.allocations[0]
    half = replace(original, allocated_quantity=ScaledDecimal.parse("1"))
    duplicate = replace(
        half,
        episode_id=episode.episode_id,
        action=AllocationAction.INCREASE,
    )
    mutated = replace(episode, allocations=(half, duplicate, *episode.allocations[1:]))
    with pytest.raises(TradingReviewError):
        verify_conservation(executions, fees, pnls, (mutated,))


def test_censored_episode_cannot_be_marked_eligible() -> None:
    request = simple_long_request()
    trade_artifact = request.artifacts[0]
    lines = trade_artifact.content.decode("utf-8-sig").splitlines()
    one_row = ("\r\n".join(lines[:2]) + "\r\n").encode()
    one_row = b"\xef\xbb\xbf" + one_row
    position_artifact = replace(request.artifacts[1], content=b"\xef\xbb\xbf" + lines[0].encode())
    censored_request = replace(
        request,
        artifacts=(replace(trade_artifact, content=one_row), position_artifact),
    )
    # Use a valid empty Position file instead of the intentionally unrelated header above.
    from tests.trading_review_support import POSITION_COLUMNS, csv_bytes

    censored_request = replace(
        censored_request,
        artifacts=(
            censored_request.artifacts[0],
            replace(censored_request.artifacts[1], content=csv_bytes(POSITION_COLUMNS, [])),
        ),
    )
    result = normalize_and_reconstruct(censored_request)
    episode = replace(
        result.episodes[0],
        eligible=True,
        reconciliation_status=ReconciliationStatus.EXACT,
    )
    executions = tuple(record for record in result.canonical_records if hasattr(record, "side"))
    fees = tuple(record for record in result.canonical_records if hasattr(record, "fee_kind"))
    pnls = tuple(record for record in result.canonical_records if hasattr(record, "realized_pnl"))
    with pytest.raises(TradingReviewError):
        verify_conservation(executions, fees, pnls, (episode,))
