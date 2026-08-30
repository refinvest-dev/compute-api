# ADR-034 — Market Data Ingestion Source and Adjustment Policy

## Status

Proposed — vendor selection and adjustment policy require product and data-owner approval.

## Context

The Engine, snapshot publisher, and worker can now consume immutable canonical Daily
Bar artifacts. No contract yet defines which vendor supplies each asset, how US equity
corporate actions are adjusted, or which exchange defines `BTCUSDT`. Those choices
change historical values and therefore Backtest Result reproducibility.

This ADR defines the decisions required before a vendor adapter is implemented. It
does not authorize a runtime fallback, implicit price adjustment, or a vendor SDK in
the Engine.

## Proposed Decision

- Use exactly one approved primary source for each asset class in a published
  DatasetSnapshot:

  | Asset class | MVP assets | Required decision |
  |---|---|---|
  | US equity / ETF / index reference | QQQ, SPY, TQQQ, SOXL, VIX | vendor, exchange-calendar source, licensing scope |
  | Crypto spot pair | BTCUSDT | exchange, symbol mapping, UTC daily-bar boundary |

- A snapshot records the chosen source identifier and source version in its immutable
  metadata and calendar provenance. The vendor response, latest endpoint, or current
  vendor configuration is never consulted while executing a Backtest.
- Ingestion canonicalizes every record to `asset`, `session_date`, `open`, `high`,
  `low`, `close`, and `volume`, then validates it before snapshot publication. Vendor
  types and field names do not cross the ingestion boundary.
- The US equity adjustment policy is explicit in `DatasetSnapshot.adjustment_policy`.
  Recommended MVP policy: split-adjusted OHLCV with cash dividends left as actual
  price movements. This preserves a consistent tradable price basis for Open-based
  execution while preventing stock-split discontinuities from fabricating returns.
- `BTCUSDT` must name one approved exchange and use that exchange's UTC daily bar:
  the session date is `[00:00:00, 24:00:00)` UTC and `completed_at` is the next UTC
  midnight. A different exchange or daily boundary produces a different source and
  therefore a different snapshot; data must not be combined across them.
- Ingestion creates the explicit calendar artifact defined in ADR-019/021, validates
  it together with canonical bars, publishes the immutable artifacts under ADR-033,
  and only then exposes DatasetSnapshot metadata through the workflow boundary.
- Vendor outage, rate limit, malformed response, or missing expected bar fails the
  ingestion attempt. Ingestion never fills gaps from a second vendor or creates a
  synthetic bar. A later ingestion run may publish a new snapshot only after a
  complete successful validation.

## Required Approval

1. Approved US equity/ETF/VIX data vendor and exchange-calendar source.
2. Approved `BTCUSDT` exchange and exact vendor symbol mapping.
3. Whether the recommended split-adjusted, non-dividend-adjusted policy is accepted,
   or which alternative (`RAW`, split-and-dividend-adjusted, etc.) is required.
4. Vendor licensing, retention, redistribution, and historical backfill limits.

## Consequences

- A subsequent small PR can implement one vendor-neutral raw-to-canonical adapter
  for the approved source without changing Engine semantics.
- Changing source, exchange, calendar rules, or adjustment policy creates a new
  DatasetSnapshot; no existing snapshot is rewritten.
- Snapshot metadata must make source and adjustment policy visible to data operators
  and reproducibility investigations.
