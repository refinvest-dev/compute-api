# ADR-047: Snapshot Publication Observability

## Status

Accepted

## Context

The API can remain available while the daily snapshot scheduler stops publishing new market data.
The immutable snapshot lifecycle already prevents partial publication and silent fallback, but
operators need a dependable signal for publication progress and failure.

## Decision

The `refinvest-publish-snapshot` process emits one JSON journal event when publication starts and
exactly one terminal JSON event when the started publication completes or fails:

- `snapshot_publication_started`
- `snapshot_publication_completed`
- `snapshot_publication_failed`

Events include the snapshot ID, coverage start/end dates, storage path, and terminal publication
duration. Failure events include only the exception type; they do not include exception messages,
credentials, or vendor response payloads. The console entrypoint suppresses Python tracebacks for
these failures while preserving a non-zero exit code, so the journal does not reintroduce a vendor
error message outside the structured event.

The deployment monitoring route alerts when it observes a failure event or no completed event for
the scheduled daily run. The alert destination and freshness threshold remain infrastructure
configuration because they depend on the deployed timer, maintenance windows, and on-call policy.
The application does not create a mutable "last success" record and does not alter snapshot
selection, retry, or failure behavior.

## Consequences

- Operators can distinguish a scheduler absence, an in-progress publication, and a terminal
  publication failure from structured logs.
- The latest published DatasetSnapshot remains the sole durable source of successful publication
  state; failed attempts never become visible through reference-data APIs.
- Backtest and Data Explorer determinism are unchanged.
