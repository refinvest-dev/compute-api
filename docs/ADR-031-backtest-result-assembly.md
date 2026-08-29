# ADR-031 — Backtest Result Assembly Boundary

## Status

Accepted for the Daily Bar MVP.

## Decision

- The Engine assembles `PerformanceReport`, realized `Trade` values, and
  `BenchmarkReferences` into one immutable `AssembledBacktestResult`.
- Assembly does not recompute performance or benchmark values. It validates that
  `PerformanceMetrics.trade_count` matches the realized trade count and derives
  `sample_size_warning` only from those realized trades.
- Snapshot integrity is explicit input from the snapshot-resolution/application
  boundary: `dataset_snapshot_id`, `corporate_actions_applied`, and
  `point_in_time_validation_passed`. The Engine does not infer these values.
- For zero trades, performance ratios and trade-derived statistics must remain
  absent, as decided by ADR-029. Assembly rejects fabricated zero-trade values.
- `runId`, `signalExecutionDelay`, completed-at timestamp conversion, JSON number
  conversion, and durable `COMPLETED` job updates are outside the pure Engine
  assembly boundary.

## Consequences

- The complete calculation result is deterministic and independent of FastAPI,
  PostgreSQL, worker lifecycle, and result transport.
- A later application/transport slice owns OpenAPI response construction using
  calendar completion timestamps and explicit snapshot provenance.
