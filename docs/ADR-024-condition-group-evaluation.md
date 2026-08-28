# ADR-024 — Condition Group Evaluation

## Status

Accepted for the Daily Bar MVP.

## Context

The MVP DSL represents a single, non-nested Condition Group as an ordered
sequence. Each condition after the first carries an `AND` or `OR` combinator.
Allowing both combinators in that flat representation would leave precedence
ambiguous. Short-circuit evaluation could also suppress a required-data or
calendar failure in a later condition, violating the fail-fast data-integrity
contract.

Daily Bar metrics are `Decimal` values. Literal operands must preserve that
numeric basis instead of relying on binary floating-point comparisons.

## Decision

- A Condition Group contains one or more conditions. Its first condition has no
  combinator; every later condition has a combinator.
- All later combinators in one MVP group must be identical: all `AND` or all
  `OR`. A mixed group is rejected during DSL validation with `DSL_INVALID`.
- The evaluator resolves every condition operand in declared order before
  combining Boolean results. It does not short-circuit. Metric, calendar, and
  market-data failures therefore always propagate.
- The evaluator then combines Boolean results in declared order using the
  group's single combinator.
- Metric values and literal operands are represented as `Decimal` in the
  Engine. Comparisons are exact `LT`, `GT`, `LTE`, or `GTE` comparisons; no
  tolerance is applied.
- Nested groups, parentheses, and mixed-combinator precedence are outside the
  MVP. They require a future DSL contract change.

## Consequences

- The condition result is deterministic for the same pinned snapshot and DSL.
- A true early `OR` condition cannot hide corruption or missing data in a later
  condition.
- Literal decimal values are persisted in the internal request payload as
  strings so standard JSON storage does not coerce them through binary floats.
