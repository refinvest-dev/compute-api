# ADR-022 — Daily Bar Metric Price Basis

## Status

Proposed for the Daily Bar MVP.

## Context

The DSL supports `SIMPLE`, `RETURN`, and `CHANGE` metric references. The
temporal contract now resolves each referenced asset to a completed,
snapshot-pinned Daily Bar session, but the canonical documents do not specify
which Daily Bar field is the metric value. `Open` is defined only as the
execution reference price.

Selecting `open`, `high`, or `low` would either use a value available before
session completion or introduce an intraday ordering assumption that the Daily
Bar MVP cannot represent. This choice therefore affects point-in-time
correctness and Backtest reproducibility.

## Decision

- The Daily Bar value for `SIMPLE` is the selected anchor session's canonical
  `DailyBar.close`.
- The same `close` price basis will apply to the future `RETURN` and `CHANGE`
  implementations; their lookback windows remain defined over the referenced
  asset's own expected-session sequence.
- Metric evaluation may use a bar only after that session's immutable
  `completed_at`. Cross-calendar anchoring remains responsible for selecting
  the latest completed session at the Primary Signal Timestamp.
- `DailyBar.open` remains exclusive to Entry/Exit execution pricing and must
  not be substituted for a metric value.
- The selected `EXPECTED` session lacking a Daily Bar is
  `MISSING_REQUIRED_DATA`. A missing calendar classification or no completed
  anchor remains `CALENDAR_RESOLUTION_FAILED`.

## Consequences

- A condition evaluated on a completed session observes its closing value and
  can only execute on a later available Execution Asset session.
- The future evaluator can be a pure function of the pinned `MarketDataPort`,
  the immutable calendar artifact, and the metric reference; it will not need
  vendor data, a live exchange schedule, or FastAPI.
