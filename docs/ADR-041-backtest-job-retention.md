# ADR-041 — Backtest Job Retention and Cleanup

## Status

Accepted for the Daily Bar MVP.

## Decision

- Compute retains terminal (`COMPLETED` and `FAILED`) backtest jobs, their runtime result
  or failure payload, request payload, and idempotency data for 24 hours after terminal
  transition. Compute does not provide long-term result retention.
- `terminal_at` is recorded atomically with every terminal transition. `completed_at`
  remains the completion-result timestamp and is not used as a failure timestamp.
- A dedicated cleanup timer invokes Compute hourly. One invocation deletes at most 1,000
  eligible rows using PostgreSQL's current time; it deletes only terminal rows whose
  `terminal_at` is at least 24 hours old. It never deletes `PENDING` or `RUNNING` jobs.
- Core copies terminal results into its own long-term store before Compute retention
  expires. After cleanup, `GET /backtests/{runId}` returns `404`; it means the run is
  unknown to Compute or its runtime retention has expired.
- Core must not retry a logical submission after the 24-hour idempotency horizon. A new
  user-initiated run uses a new `Idempotency-Key`.
- Cleanup emits the deleted row count. Operations alert on cleanup failures and on
  terminal jobs older than the retention horizon that remain after a successful cleanup.

## Consequences

- Job retention and idempotency retention have the same boundary, so cleanup cannot retain
  a replay key without the original accepted run.
- A failed job has an explicit retention clock; no implicit use of `created_at` is needed
  for newly terminal jobs. The migration backfills existing terminal rows with
  `completed_at` where available, otherwise `created_at`, solely to make the new invariant
  valid for historical runtime rows.
- Retention cleanup does not change dataset snapshot artifacts or backtest determinism.
