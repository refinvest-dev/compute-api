# ADR-033 — Snapshot Artifact Storage Format

## Status

Accepted for the Daily Bar MVP.

## Context

`DatasetSnapshot` metadata has an immutable `storage_path` and calendar provenance,
but the persisted artifact format and access boundary are not defined. The concrete
`DailyBarBacktestExecutor` now requires a `SnapshotExecutionDataLoader` to reconstruct
the pinned calendar and Daily Bars. Selecting a format without a contract would leave
the following reproducibility-critical behavior implicit:

- the exact artifact set covered by `storage_path`;
- validation of artifact identity and integrity before Engine execution;
- representation of `EXPECTED`/`NON_TRADING` sessions and immutable `completed_at`;
- Decimal and date/timestamp parsing rules for canonical Daily Bars;
- mapping unavailable or malformed artifacts to fatal errors.

## Decision

- `storage_path` identifies one immutable snapshot artifact directory/prefix. It is
  never overwritten after the corresponding `DatasetSnapshot` metadata is published.
- Each prefix contains `manifest.json`, one calendar JSON artifact, and one Daily Bar
  NDJSON artifact. A snapshot with a close-only reference asset additionally contains
  a Reference Close NDJSON artifact. New artifacts use `formatVersion: 2`; the loader
  remains compatible with v1. The manifest has `snapshotId`,
  `corporateActionsApplied`, `pointInTimeValidationPassed`, and `calendar` / `dailyBars`
  entries and, when present, a `referenceCloses` entry. Each artifact entry has a
  relative `path` and SHA-256 `sha256` hash.
- The manifest is the atomic loader entry point. A loader reads and validates it
  before exposing any calendar or market-data adapter to the Engine.
- Calendar JSON contains a `provenance` object (`source`, `version`, `contentHash`) and
  a `sessions` array. Each record preserves `asset`, `sessionDate`, `state`, and, for
  `EXPECTED`, an exact UTC `completedAt` timestamp. Calendar provenance must exactly match the
  `DatasetSnapshot` metadata, as required by ADR-019 and ADR-021.
- Each Daily Bar NDJSON record preserves canonical `asset`, `sessionDate`, `open`, `high`, `low`,
  `close`, and `volume`. In v2, all Decimal fields including `volume` are encoded as
  decimal strings, never JSON binary numbers. v1 integer volume remains supported for
  immutable historical snapshot replay under ADR-035.
- Each Reference Close NDJSON record preserves only `asset`, `sessionDate`, and `close`.
  It is used for non-executable assets such as VIX; the loader must not infer an OHLCV
  Daily Bar from it.
- A missing manifest/artifact or a hash mismatch is `DATASET_CORRUPTION`. An expected
  session without a Daily Bar remains `MISSING_REQUIRED_DATA`; it is not converted
  into an artifact-load failure.
- The initial adapter reads a local filesystem directory from the pinned snapshot's
  `storage_path`. A future object-storage adapter must preserve this manifest contract.
  The Engine remains independent of filesystem paths, object-storage SDKs, and mutable
  latest data.

## Consequences

- A filesystem `SnapshotExecutionDataLoader` can be implemented without adding vendor
  or object-storage dependencies to the Engine.
- Ingestion must publish artifacts before, or atomically with, snapshot metadata so a
  claimed snapshot can always be reconstructed.
- Integration tests must cover hash mismatch, calendar provenance mismatch, malformed
  UTC completion timestamps, and expected-session Daily Bar gaps.
