# ADR-019 — Immutable Snapshot Calendar Policy

## Status

Accepted for the Daily Bar MVP.

## Decision

- QQQ, SPY, TQQQ, SOXL, and VIX use `US_EQUITY`; their Daily Bar `session_date` is an `America/New_York` trading date.
- `US_EQUITY` calendar artifacts use the version-pinned `exchange_calendars` `XNYS`
  schedule as their baseline. NYSE and Nasdaq official holiday/early-close notices
  are explicit immutable overrides; the package version and canonical override hash
  are stored as calendar provenance. No live calendar endpoint is read by the Engine.
- BTCUSDT uses `CRYPTO_UTC`; its Daily Bar `session_date` is a UTC date and every date is an expected session.
- Daily Bar ingestion creates an immutable calendar artifact for each DatasetSnapshot. The artifact records explicit `EXPECTED` and `NON_TRADING` classifications by AssetSession plus source, version, and content hash.
- DatasetSnapshot metadata stores the calendar artifact provenance. A snapshot-backed Market Data adapter rejects an artifact whose provenance does not exactly match its snapshot metadata.
- A missing calendar classification is `CALENDAR_RESOLUTION_FAILED`. An `EXPECTED` session without a Daily Bar is `MISSING_REQUIRED_DATA`. Neither condition may use fallback, interpolation, or another snapshot.
- Early-close dates are `EXPECTED` sessions for the Daily Bar MVP. Intraday session timing is out of scope.

## Consequences

- Backtests replay the same calendar decisions with the same DatasetSnapshot.
- The Engine consumes calendar classifications; it does not call a holiday API or derive current exchange schedules at runtime.
- Ingestion owns the future exchange-calendar source and artifact generation implementation.
