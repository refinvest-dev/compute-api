# ADR-032 — Durable Completed Backtest Results

## Status

Accepted for the Daily Bar MVP.

## Decision

- Compute stores completed results only as polling runtime state; Core owns long-term result retention.
- A lease owner alone may transition `RUNNING` to `COMPLETED`; the transition stores canonical JSONB result payload, actual period, engine version, and completion time atomically.
- Canonical runtime payloads preserve Decimal values as strings and timestamps as UTC ISO-8601 strings.
- `signalExecutionDelay` remains absent until its semantics are defined.

## Consequences

- Lease loss cannot overwrite a newer attempt's terminal state.
- API number presentation and timestamp transport remain application mapping concerns.
