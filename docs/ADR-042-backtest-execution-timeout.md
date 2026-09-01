# ADR-042 — Backtest Execution Deadline and Process Isolation

## Status

Accepted for the Daily Bar MVP.

## Decision

- One snapshot-pinned execution attempt has a 10-minute wall-clock deadline, measured from
  starting its isolated child process through result production.
- The lease-owning worker runs engine execution in a child process. Only the parent worker
  writes `COMPLETED` or `FAILED` state to PostgreSQL; the child returns an outcome through
  local IPC and never owns a database transition.
- On deadline expiry the parent sends termination, waits up to 30 seconds, then force-kills
  the child if necessary. It records terminal `FAILED` with `EXECUTION_TIMEOUT` and does
  not schedule the normal transient retry sequence.
- The existing 60-second lease and 15-second heartbeat continue while the child runs.
  They recover parent-worker failure; they do not define the execution deadline.
- Timeout logs include run ID, dataset snapshot ID, attempt number, and timeout seconds.

## Consequences

- A non-cooperative engine/data-loader call cannot keep a worker busy indefinitely.
- A timed-out child cannot later overwrite terminal state because it has no job repository.
- The timeout applies to immutable snapshot reads and engine execution only; queue wait is
  reported by `PENDING` state and is not part of this deadline.
- `EXECUTION_TIMEOUT` is an operational terminal failure, added to the Compute error-code
  contract. A new user-initiated run must use a new idempotency key.
