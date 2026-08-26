# ADR-018 — Backtest Worker Lifecycle

## Status

Accepted for the initial worker foundation.

## Decision

- A PostgreSQL transaction atomically claims one eligible job. Public status transitions only move `PENDING → RUNNING → COMPLETED | FAILED`.
- Workers use `worker_id`, `lease_expires_at`, and `heartbeat_at`. A stale `RUNNING` job is reclaimed without returning it to `PENDING`.
- Snapshot resolution occurs during claim. The explicit requested snapshot is verified; otherwise the latest snapshot is selected once. The selected ID is pinned before work begins.
- Snapshot/data integrity failures are terminal `FAILED` and are never retried. Transient infrastructure failures retry the same pinned snapshot at most three times with 30-second, 2-minute, and 10-minute delays.
- Terminal jobs are retained for 24 hours. `PENDING` and `RUNNING` jobs are excluded from cleanup.

## Consequences

- Engine inputs remain immutable across retries and worker recovery.
- Worker claim recovery requires lease and retry metadata in the Compute-owned job table.
- `POST /backtests` currently has no idempotency key. Until the OpenAPI contract adds a Core-generated `requestId` or `Idempotency-Key`, retried POST requests create independent jobs; no implicit deduplication is allowed.
