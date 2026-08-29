# ADR-028 — Trade Realization and Sample Size

## Status

Accepted for the Daily Bar MVP.

## Decision

- A `Trade` is realized only for a `PlannedPosition` admitted under ADR-027.
- `EntryExecution.entry_price` and `ExitExecution.exit_price` remain the prices after
  slippage and before commission. They are the prices reported by the trade.
- `FeeModel.commission` applies independently to each side's notional:

  ```text
  entry_cost    = entry_price * (1 + commission)
  exit_proceeds = exit_price * (1 - commission)
  return_pct    = (exit_proceeds / entry_cost) - 1
  ```

- `commission` and `slippage` must each be in `[0, 1)`. Trade realization rejects
  values outside that range.
- All realization arithmetic uses ADR-025's local `Decimal` context and does not
  round intermediate values.
- A trade's holding period is the configured `holdingSignalSessions`, measured on
  the Primary Signal Asset calendar. It is not the difference between execution
  asset dates.
- Sample-size warnings are derived solely from the realized trade count:

  ```text
  0 trades       -> ZERO
  1 through 9    -> LOW
  10 or more     -> NONE
  ```

## Consequences

- Cross-calendar strategies preserve the strategy's declared holding-session
  meaning even when execution sessions occur on a different calendar.
- Zero trades are a completed, valid engine outcome; no undefined statistics are
  converted to zero in this slice.
- Equity curve, aggregate performance metrics, benchmark, and API transport time
  mapping remain separate later slices because they require additional policies.
