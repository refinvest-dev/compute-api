## Summary

- Add a concrete Daily Bar `BacktestJobExecutor` that runs the deterministic Engine pipeline from a pinned `DatasetSnapshot`.
- Restore canonical job payloads without using FastAPI transport models, preserving Decimal values stored as strings.
- Resolve the actual period from the requested period and the snapshot availability intersection of all strategy-relevant assets.
- Serialize completed runtime results with Decimal strings and UTC ISO-8601 timestamps.
- Add an end-to-end regression test for a BTC weekend signal executing at the next available TQQQ session.

## Behavior

- The executor only receives data through `SnapshotExecutionDataLoader`; it does not use vendor APIs or latest mutable data.
- The loader result must identify the same snapshot ID pinned to the claimed job.
- Calendar and expected-session data failures are terminal and mapped to the documented fatal error categories.
- Expected-session data gaps remain fail-fast and are never filled or substituted.

## Validation

- `uv run pytest` — 141 passed, 2 skipped
- `uv run ruff check .` — passed
- `uv run pyright` — 0 errors

## Follow-up

- Implement the ingestion/object-storage-backed `SnapshotExecutionDataLoader`.
- Compose and run the worker process in deployment infrastructure.
- Add PostgreSQL-backed end-to-end worker execution coverage once immutable snapshot artifacts are persisted.

## Determinism / Point-in-Time

- Determinism impact: changed — completed results are now assembled from one pinned snapshot through the full Engine pipeline.
- Point-in-Time impact: changed — signals, metrics, and execution scheduling use only the pinned snapshot calendar and market data.
