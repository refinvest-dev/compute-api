# ADR-018 — Backtest Worker Lifecycle

## Status

Accepted for the initial worker foundation.

## Decision

- A PostgreSQL transaction atomically claims one eligible job. Public status transitions only move `PENDING → RUNNING → COMPLETED | FAILED`.
- Workers use `worker_id`, `lease_expires_at`, and `heartbeat_at`. A stale `RUNNING` job is reclaimed without returning it to `PENDING`.
- Snapshot resolution occurs during claim. The explicit requested snapshot is verified; otherwise the latest snapshot is selected once. The selected ID is pinned before work begins.
- Snapshot/data integrity failures are terminal `FAILED` and are never retried. Transient infrastructure failures retry the same pinned snapshot at most three times with 30-second, 2-minute, and 10-minute delays.
- Terminal job retention and cleanup are defined by ADR-041. `PENDING` and `RUNNING` jobs
  are excluded from cleanup.

## Consequences

- Engine inputs remain immutable across retries and worker recovery.
- Worker claim recovery requires lease and retry metadata in the Compute-owned job table.
- `POST /backtests` idempotency is defined by ADR-040. Core retries preserve its
  `Idempotency-Key`; Compute never infers deduplication from request content alone.
- Backtest attempt deadlines and process isolation are defined by ADR-042.
- Worker operational events and warning thresholds are defined by ADR-043.
