# ADR-045 — Compute Asset Reference Registry

## Status

Accepted for the Daily Bar MVP.

## Decision

- Compute owns the static MVP registry: symbol, calendar, execution eligibility, and
  `inceptionDate`. Core consumes it; Web reaches it only through Core.
- `inceptionDate` replaces the ambiguous `listingDate`: it is an ETF's inception/trading
  start, the approved Binance BTCUSDT pair start, or the VIX index inception date. The
  registry is versioned in code and never queried from a vendor at request time.
- `dataAvailability` is derived from exactly one latest published immutable DatasetSnapshot
  and its manifest-verified calendar artifact. It is distinct from `inceptionDate`.
- `/assets` always returns the six supported registry entries. If no snapshot exists, their
  availability is absent. `/assets/{symbol}/availability` returns `404` for an unsupported
  symbol and `503 DATASET_UNAVAILABLE` for a supported symbol without current snapshot data.
- Responses identify the selected snapshot ID and creation time so Core can cache and trace
  the reference-data version. This endpoint does not validate or select a backtest snapshot.

## Sources

- QQQ: Invesco product detail, 1999-03-10.
- SPY: State Street product detail, 1993-01-22.
- TQQQ and SOXL: Direxion fund product materials, 2010-02-09 and 2010-03-11.
- BTCUSDT: approved Binance spot-pair history start, 2017-08-17.
- VIX: Cboe VIX launch history, 1993-01-19.

## Consequences

- No runtime market-data or calendar lookup uses mutable latest vendor state.
- Availability is reference UI data only; every backtest still resolves and validates its
  own immutable snapshot before execution.
