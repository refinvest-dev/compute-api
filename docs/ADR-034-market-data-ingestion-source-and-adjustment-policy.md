# ADR-034 — Market Data Ingestion Source and Adjustment Policy

## Status

Accepted for the Daily Bar MVP.

## Context

The Engine, snapshot publisher, and worker can now consume immutable canonical Daily
Bar artifacts. No contract yet defines which vendor supplies each asset, how US equity
corporate actions are adjusted, or which exchange defines `BTCUSDT`. Those choices
change historical values and therefore Backtest Result reproducibility.

This ADR defines the decisions required before a vendor adapter is implemented. It
does not authorize a runtime fallback, implicit price adjustment, or a vendor SDK in
the Engine.

## Decision

- Use exactly one approved primary source for each asset class in a published
  DatasetSnapshot:

  | Asset class | MVP assets | Required decision |
  |---|---|---|
  | US ETF | QQQ, SPY, TQQQ, SOXL | Massive EOD US stocks |
  | US volatility index reference | VIX | Cboe official historical VIX close series |
  | Crypto spot pair | BTCUSDT | Binance Spot `BTCUSDT`, UTC daily bar |

- A snapshot records the chosen source identifier and source version in its immutable
  metadata and calendar provenance. The vendor response, latest endpoint, or current
  vendor configuration is never consulted while executing a Backtest.
- Ingestion canonicalizes Daily Bar records to `asset`, `session_date`, `open`, `high`,
  `low`, `close`, and `volume`, then validates them before snapshot publication.
  VIX close-only records preserve only `asset`, `session_date`, and `close`. Vendor
  types and field names do not cross the ingestion boundary.
- The US ETF adjustment policy is explicit in `DatasetSnapshot.adjustment_policy`:
  split-adjusted OHLCV with cash dividends left as actual
  price movements. This preserves a consistent tradable price basis for Open-based
  execution while preventing stock-split discontinuities from fabricating returns.
- `BTCUSDT` uses Binance Spot `BTCUSDT` and that exchange's UTC daily bar:
  the session date is `[00:00:00, 24:00:00)` UTC and `completed_at` is the next UTC
  midnight. A different exchange or daily boundary produces a different source and
  therefore a different snapshot; data must not be combined across them.
- Ingestion creates the explicit calendar artifact defined in ADR-019/021, validates
  it together with canonical bars, publishes the immutable artifacts under ADR-033,
  and only then exposes DatasetSnapshot metadata through the workflow boundary.
- VIX is a close-only reference series. It must not fabricate Open/High/Low/volume
  values from Cboe's close series; execution remains prohibited and all VIX metrics
  use its immutable session close.
- Vendor outage, rate limit, malformed response, or missing expected bar fails the
  ingestion attempt. Ingestion never fills gaps from a second vendor or creates a
  synthetic bar. A later ingestion run may publish a new snapshot only after a
  complete successful validation.

## Consequences

- A subsequent small PR adds a VIX close-only reference artifact and Engine data port
  support without fabricating an OHLCV bar.
- After that, separate small PRs implement Massive ETF and Binance Spot raw-to-canonical
  ingestion adapters.
- Changing source, exchange, calendar rules, or adjustment policy creates a new
  DatasetSnapshot; no existing snapshot is rewritten.
- Snapshot metadata must make source and adjustment policy visible to data operators
  and reproducibility investigations.
