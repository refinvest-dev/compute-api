# ADR-044 — Backtest Admission Capacity

## Status

Accepted for the Daily Bar MVP.

## Decision

- Compute permits at most 20 active jobs globally. Active means `PENDING` or `RUNNING`,
  including a `RUNNING` job waiting for its bounded transient retry. Terminal jobs do not
  consume admission capacity.
- A new submission at capacity receives `503 Service Unavailable` with
  `errorCode: BACKTEST_CAPACITY_EXCEEDED` and `Retry-After: 30`. Compute never holds an HTTP
  request open while capacity becomes available.
- A repeated `Idempotency-Key` is resolved before capacity. Same key and canonical request
  returns its original `202/runId` even when full; a different payload remains `409`.
  A capacity refusal creates neither a job nor an idempotency record, so Core may retry the
  same logical request and key after the advertised delay.
- PostgreSQL serializes idempotency lookup, active-job counting, and insertion under a
  transaction-scoped advisory lock. This prevents concurrent acceptance from exceeding the
  global limit.
- Core owns per-user rate limits, quota, and priority. Compute applies no user-specific
  policy and preserves FIFO claim ordering among accepted jobs.
- The configured limit defaults to 20 and is positive-only. Increasing worker count requires
  an explicit capacity review; repeated 10-minute queue-wait warnings are the initial signal.

## Consequences

- Compute rejects overload early without allocating durable runtime state for work it cannot
  accept.
- Admission changes neither snapshot selection nor engine semantics. It only decides whether
  a new durable execution request may enter the global queue.
