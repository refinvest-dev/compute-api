# ADR-036 — Composite Snapshot Calendar Provenance

## Status

Accepted for the Daily Bar MVP.

## Context

One complete MVP `DatasetSnapshot` may combine `US_EQUITY` assets (QQQ, SPY,
TQQQ, SOXL, VIX) and the `CRYPTO_UTC` asset (BTCUSDT). ADR-019 requires calendar
provenance to match the immutable snapshot metadata, while the current metadata has
one `calendarSource`, `calendarVersion`, and `calendarHash` tuple. Flattening the
two component provenance values into strings would make source versions and hashes
ambiguous during replay or an operational investigation.

## Decision

- A complete multi-calendar snapshot publishes one composite `calendar.json`
  artifact. Its top-level provenance is:

  ```text
  source  = composite-calendar
  version = 1
  hash    = SHA-256 of canonical component provenance plus canonical sessions
  ```

  The top-level tuple is copied exactly to the `DatasetSnapshot` calendar metadata
  and continues to be the tuple the snapshot-backed adapter validates.
- The composite artifact contains a deterministic `components` array in addition
  to `provenance` and `sessions`. Each component records its calendar identity,
  owned assets, and its own source/version/hash provenance. The MVP components are:

  | Calendar | Assets | Component provenance |
  |---|---|---|
  | `US_EQUITY` | QQQ, SPY, TQQQ, SOXL, VIX | pinned XNYS schedule plus official overrides |
  | `CRYPTO_UTC` | BTCUSDT | fixed every-UTC-date rule |

- The composite `calendarHash` is the SHA-256 of UTF-8 canonical JSON containing
  the sorted component records and sorted sessions. The hash input excludes the
  top-level composite provenance itself, preventing a self-referential hash. A
  component source/version/hash, component asset ownership, session state, or
  `completedAt` change therefore creates a different snapshot calendar identity.
- Every MVP asset in a composite snapshot must be owned by exactly one component.
  A duplicate, unsupported, or unclassified asset, and a session that does not
  belong to its component, fails snapshot publication. Components and sessions are
  sorted by calendar/asset/date before hashing and serialization.
- Existing v1/v2 single-calendar artifacts remain replayable. A reader treats a
  missing `components` field as the historical single-provenance format. New
  publishers emit the composite structure only when more than one calendar is
  present; no historical artifact or snapshot metadata is rewritten.
- The Engine continues to consume only the loaded, immutable `AssetSession`
  classifications and completion timestamps. It neither interprets component
  provenance nor calls a calendar or vendor endpoint at backtest time.

## Consequences

- The following implementation PR extends calendar artifact serialization and
  loading with the optional composite components and validates the canonical hash.
- A later, separate ingestion-orchestration PR can safely combine the Massive,
  Cboe, and Binance adapters with the US-equity and crypto-UTC calendar builders.
- Operators can identify both the aggregate snapshot calendar and each contributing
  source rule without relying on an ambiguous concatenated metadata string.
