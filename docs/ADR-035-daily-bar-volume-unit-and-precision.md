# ADR-035 — Daily Bar Volume Unit and Precision

## Status

Accepted for the Daily Bar MVP.

## Context

The canonical `DailyBar` model originally represented `volume` as an integer, which
is valid for US ETF share volume. Binance Spot Klines represent BTCUSDT base-asset
volume as a decimal string. Rounding, truncating, zeroing, or omitting that value
would silently change vendor data before an immutable DatasetSnapshot is published.

## Decision

- `DailyBar.volume` is a non-negative `Decimal` in the asset's native base unit.
  ETF volumes therefore remain integer-valued Decimals; BTCUSDT volume may contain a
  fractional BTC quantity.
- Snapshot artifact format v2 encodes `DailyBar.volume` as a decimal string, matching
  the existing price-field encoding. New publishers always emit v2.
- The loader continues to replay v1 artifacts whose `volume` is a JSON integer by
  converting that exact integer to `Decimal`. It parses v2 volume only from a decimal
  string. Existing v1 artifact hashes and snapshot behavior remain unchanged.
- Volume is preserved as canonical market-data provenance. The MVP Engine does not
  use it to derive prices, signals, execution, or returns.

## Consequences

- Binance Spot Kline base volume can enter a DatasetSnapshot without loss of precision.
- Any future vendor adapter must state whether its supplied volume is in base units and
  reject values that cannot be represented exactly as a non-negative `Decimal`.
- Artifact readers must retain v1/v2 compatibility until v1 snapshots are no longer
  supported operationally.
