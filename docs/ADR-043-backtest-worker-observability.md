# ADR-043 — Backtest Worker Operational Observability

## Status

Accepted for the Daily Bar MVP.

## Decision

- Compute emits JSON-formatted, one-event-per-line logs to the worker and cleanup systemd
  journals. It does not introduce a separate metrics service or always-on agent.
- Worker events include job claimed, stale lease reclaimed, queue wait warning, execution
  duration warning, completed, terminal failed, retry scheduled, and retry exhausted.
  Each job event includes run ID, worker ID, snapshot ID, and attempt number.
- The warning thresholds are 10 minutes of queue wait and 5 minutes of execution duration.
  `EXECUTION_TIMEOUT` and retry exhaustion are warning events regardless of duration.
- Cleanup emits `backtest_cleanup_completed` with the bounded deleted row count. systemd
  service failure remains the cleanup-failure signal.
- Infrastructure forwards the existing systemd journals to CloudWatch Logs (or an equivalent
  deployment-owned sink) and alerts on warning events, cleanup service failure, and terminal
  jobs older than the ADR-041 retention boundary after cleanup.

## Consequences

- Observability adds no market-data lookup, no mutable engine input, and no execution-path
  fallback; determinism and point-in-time behavior remain unchanged.
- Queue wait is derived from the durable job `created_at` and PostgreSQL claim data. It is an
  operational signal only and does not change scheduling order.
- Log forwarding, retention, alert destinations, and CloudWatch resources remain
  infrastructure-owned rather than application credentials or SDK configuration.
