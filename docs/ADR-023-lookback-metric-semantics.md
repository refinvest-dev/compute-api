# ADR-023 — Lookback Metric Semantics

## Status

Accepted for the Daily Bar MVP.

## Context

ADR-022 fixes every Daily Bar metric value to the canonical `close` of a
snapshot-pinned session. `RETURN` and `CHANGE` still require an exact lookback
definition, formulas, precision rules, and failure taxonomy. Date arithmetic
would be incorrect across weekends, holidays, and cross-calendar strategies;
the referenced asset's immutable expected-session sequence is the only valid
window source.

## Decision

- Resolve the referenced asset's metric anchor first, using the Primary Signal
  Asset Signal Timestamp and ADR-021. Let its position in the referenced
  asset's ordered `EXPECTED` session sequence be `t`.
- For `window = n`, the comparison session is exactly `t - n`; the anchor is
  not counted as one of the `n` sessions. Thus `window = 1` compares the
  anchor with its immediately preceding referenced-asset expected session.
- `RETURN = (close_t / close_t-n) - 1`.
- `CHANGE = close_t - close_t-n`. It is an absolute price difference, not a
  percentage, so it remains distinct from `RETURN`.
- Both values use canonical `DailyBar.close`. Metric calculations use
  `Decimal` under a local, fixed `Context(prec=28, rounding=ROUND_HALF_EVEN)`
  and do not round intermediate or comparison values.
- If the anchor cannot be resolved, or fewer than `n` earlier expected
  sessions exist in the pinned calendar artifact, fail with
  `CALENDAR_RESOLUTION_FAILED`.
- If either resolved expected session has no Daily Bar, fail with
  `MISSING_REQUIRED_DATA`. A non-positive close used as a `RETURN` denominator
  is `DATASET_CORRUPTION`; it must never result in division by zero or a
  fabricated value.

## Consequences

- Weekends and exchange holidays never change a configured lookback length.
- Cross-calendar timing is resolved once at the anchor; all lookback movement
  remains on the referenced asset's own calendar.
- The future evaluator remains a pure function of `MetricReference`, the
  selected immutable calendar artifact, and the pinned `MarketDataPort`.
