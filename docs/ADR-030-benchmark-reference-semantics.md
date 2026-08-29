# ADR-030 — Benchmark Reference Semantics

## Status

Accepted for the Daily Bar MVP.

## Decision

- A benchmark is a cost-free market reference, not an executable order simulation.
  It does not apply `FeeModel.commission` or `FeeModel.slippage`.
- The primary benchmark is the Execution Asset. A secondary reference is the
  Primary Signal Asset only when it differs from the Execution Asset.
- Each reference uses that asset's own pinned `EXPECTED` sessions that fall
  inclusively inside the actual period. It never substitutes a session outside
  the period.
- Reference values are normalized Close-to-Close values:

  ```text
  value_t      = close_t / first_close
  total_return = last_close / first_close - 1
  mdd          = min(value / running_peak - 1)
  cagr         = last_value ** (365.25 / calendar_days) - 1
  ```

  `calendar_days` is the difference between the first and last reference
  session dates. A zero-day span has no CAGR.
- Every expected reference session in the actual period requires a Daily Bar.
  Missing data fails fast. A non-positive Close is `DATASET_CORRUPTION`; no
  session in the period is `CALENDAR_RESOLUTION_FAILED`.
- `secondary_reference` is a market reference, not an assertion that the asset
  is executable. Therefore VIX is included when it is the cross-market Primary
  Signal Asset, despite being prohibited as an Execution Asset.

## Consequences

- Strategy results retain fee/slippage-inclusive net performance while benchmark
  results express the underlying market movement without transaction costs.
- Cross-calendar strategies retain each asset's own market path over the same
  actual-date range.
- Core and Web must label a VIX secondary reference as a signal-asset market
  reference rather than an investable Buy & Hold recommendation.
