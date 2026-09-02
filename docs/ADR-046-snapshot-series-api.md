# ADR-046: Immutable Snapshot Series API

## Status

Accepted

## Context

Core needs a Data Explorer source for comparing the MVP asset universe. Using vendor latest data
would make a chart change between requests and would violate Compute's ownership of market
reference data. Asset calendars also differ: crypto has weekend sessions while US equity does not.

## Decision

`GET /series` is a Core-only endpoint that resolves exactly one immutable `DatasetSnapshot` at
request start. A caller can supply `datasetSnapshotId`; without it, Compute selects the latest
published snapshot once and returns its ID, creation time, and adjustment policy in every 200
response.

The endpoint accepts 1–6 distinct supported symbols, a date range of at most ten years, and
returns at most 20,000 points. It supports these metrics:

- `PRICE`: the session close.
- `RETURN`: `(current close / immediately preceding available session close) - 1`. The preceding
  session can be before the requested range; if no predecessor exists, the first return is absent.
- `NORMALIZED`: the first available close within the requested range is 100 and subsequent values
  are `close / first close * 100`.

Each asset keeps only its own expected trading sessions. Compute does not create common dates,
copy prior values, interpolate, or otherwise fill non-trading dates. A supported asset with no
sessions in the range returns an empty point list. An expected session required for a result but
missing its close, or an invalid snapshot artifact, fails fast with `503 DATASET_CORRUPTION`.
No available latest snapshot is `503 DATASET_UNAVAILABLE`; an explicitly requested unknown
snapshot is `404`.

## Consequences

- Data Explorer results are reproducible when the same snapshot ID and query are repeated.
- Cross-calendar visual alignment is a Core/Web presentation concern; it cannot alter Compute
  values or manufacture observations.
- The endpoint is a reference-data reader and does not alter Backtest Engine semantics.
