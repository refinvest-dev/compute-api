# ADR-021 — Cross-Calendar Metric Anchor Timestamps

## Status

Proposed for the Daily Bar MVP.

## Context

`DOMAIN.md` requires a cross-calendar metric to use only the most recent
referenced-asset session that was already completed at the Primary Signal
Asset's Signal Timestamp. The current immutable calendar artifact records a
Daily Bar `session_date` and whether that date is `EXPECTED` or
`NON_TRADING`, but it does not record when an expected session completed.

Comparing `session_date` values is insufficient. A BTCUSDT UTC daily session
and a US equity New York trading session can have different completion times
while sharing, or neighboring, date labels. Deriving a fixed close time at
runtime would also make early closes and future calendar-rule changes part of
Engine behavior rather than pinned snapshot data.

## Decision

- Each `EXPECTED` `AssetSession` in a DatasetSnapshot calendar artifact must
  include an immutable, timezone-aware UTC `completed_at` instant. A
  `NON_TRADING` session has no completion instant.
- The Primary Signal Asset's Signal Timestamp is that signal session's
  `completed_at` instant.
- To anchor a metric for a referenced asset, select the referenced asset's
  latest `EXPECTED` session whose `completed_at` is less than or equal to the
  Signal Timestamp. Equality is permitted because that session is complete at
  the timestamp.
- The Engine must read these instants only from the pinned DatasetSnapshot
  artifact. It must not calculate exchange close times, infer them from date
  labels, consult a live calendar, or replace a missing value with an adjacent
  session.
- A missing completion instant, ambiguous ordering, or no referenced session
  completed by the Signal Timestamp is `CALENDAR_RESOLUTION_FAILED`. An
  `EXPECTED` selected session without its required Daily Bar remains
  `MISSING_REQUIRED_DATA`.
- `session_date` remains the immutable daily-bar label. It is not a temporal
  ordering key across asset calendars.

## Consequences

- Snapshot calendar ingestion must add `completed_at` to its artifact and
  provenance/hash calculation before cross-calendar metric evaluation is
  implemented.
- The future metric-evaluation slice can accept a snapshot-only temporal
  lookup and remain independent of FastAPI, persistence, vendor APIs, and
  mutable exchange schedules.
- Existing session navigation remains unchanged: lag and holding navigate the
  Primary Signal Asset's expected-session sequence, while execution uses the
  Execution Asset's next expected session.
