"""Episode and result capability aggregation."""

from __future__ import annotations

from refinvest.trading_review.model import CapabilitySnapshot, FuturesPositionEpisode


def aggregate_capability(
    episodes: tuple[FuturesPositionEpisode, ...],
) -> CapabilitySnapshot:
    eligible = tuple(item for item in episodes if item.eligible)
    capabilities = {
        capability for episode in episodes for capability in episode.capability.capabilities
    }
    unsupported_assets = {
        asset for episode in episodes for asset in episode.capability.unsupported_fee_assets
    }
    return CapabilitySnapshot(
        capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
        gross_pnl_available=any(item.capability.gross_pnl_available for item in eligible),
        fee_adjusted_outcome_available=any(
            item.capability.fee_adjusted_outcome_available for item in eligible
        ),
        holding_duration_available=any(
            item.capability.holding_duration_available for item in eligible
        ),
        stable_episode_time_available=any(
            item.capability.stable_episode_time_available for item in eligible
        ),
        reconciliation_available=any(item.capability.reconciliation_available for item in eligible),
        funding_inclusive_outcome_available=False,
        unsupported_fee_assets=tuple(sorted(unsupported_assets)),
    )
