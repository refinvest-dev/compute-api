# ADR-029 — Equity Curve and Performance Metrics

## Status

Accepted for the Daily Bar MVP.

## Decision

- Performance evaluation starts with normalized capital of `1` and uses only the
  resolved Execution Asset's pinned `EXPECTED` sessions within the actual period.
- Every curve session requires an Execution Asset Daily Bar, including flat
  sessions. An expected-session gap fails fast rather than carrying a prior value.
- Curve points use `DailyBar.close` after all events at that session:

  - On entry, capital buys `capital / entry_cost` units at the Open-based entry
    cost from ADR-028. The same session closes at immediately liquidatable net
    asset value: `units * close * (1 - commission)`.
  - While holding, use the same immediately liquidatable Close value.
  - On exit, sell at the Open-based `exit_proceeds` from ADR-028. The point is
    realized cash and remains constant while flat.

- The following metrics use the resulting normalized curve:

  ```text
  total_return = final_equity - 1
  mdd          = min(equity / running_peak - 1), with initial peak = 1
  cagr         = final_equity ** (365.25 / calendar_days) - 1
  ```

  `calendar_days` is the difference between the first and last curve session
  dates. A zero-day span has no CAGR.

- Sharpe uses zero risk-free rate, sample standard deviation of curve daily
  returns, and annualization factor `252` for `US_EQUITY` Execution Assets or
  `365` for `CRYPTO_UTC` Execution Assets. Fewer than two returns or zero
  standard deviation has no Sharpe.
- `win_rate` counts trades whose `return_pct > 0`; break-even trades are not
  wins. `profit_factor` uses sequential normalized realized P&L. It has no
  value when no losing trade exists.
- For zero trades, only `trade_count` is defined. All performance ratios and
  trade-derived statistics remain absent; they are not represented as zero.

## Consequences

- Equity and metrics remain deterministic functions of the immutable calendar,
  market data, realized trades, and fee model.
- Cross-calendar strategies are valued on the Execution Asset calendar without
  changing the Primary Signal Asset rules for holding periods.
- OpenAPI transport mapping, benchmark calculation, and data-integrity metadata
  remain later slices.
