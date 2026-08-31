# ADR-037 — MVP Snapshot Publication Lifecycle

## Status

Accepted for the Daily Bar MVP.

## Decision

- An external scheduler invokes `refinvest-publish-snapshot` once daily; Compute does
  not host an internal scheduler. Start the job after 00:15 UTC and retry/alert outside
  Compute when a vendor, artifact, or metadata operation fails.
- Each successful run publishes a complete immutable coverage from the fixed
  `REFINVEST_SNAPSHOT_START_DATE` through the latest completed UTC date:
  `end = now_utc.date() - 1`. The CLI rejects an explicit end that is current or future.
- Every run creates a new ID: `mvp-<coverage-end>-<run-utc>-<nonce>`. The ID is storage
  identity only; calendar provenance and canonical market data continue to identify the
  replayable dataset content.
- The MVP re-downloads the full coverage on every run. A vendor historical correction
  therefore appears only in a new snapshot; existing artifacts and metadata are never
  updated. Incremental artifact reuse is out of scope until it has its own hash and
  partial-failure policy.
- `REFINVEST_US_EQUITY_OVERRIDES_PATH` names the deployment-provided immutable UTF-8
  JSON override file. The job never fetches live exchange notices. The file is part of
  the calendar provenance through the US-equity builder's canonical override hash.

## Consequences

- A failed run exposes no new snapshot metadata and does not fall back to an older or
  secondary source.
- Operators may still explicitly provide start/end/ID for controlled backfills, subject
  to the completed-date validation.
