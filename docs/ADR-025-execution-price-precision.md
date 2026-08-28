# ADR-025 — Execution Price Precision

## Status

Accepted for the Daily Bar MVP.

## Decision

- `FeeModel.commission`과 `FeeModel.slippage`는 Engine에서 `Decimal`로 표현한다.
- Entry reference price는 pinned DatasetSnapshot의 resolved Execution Asset session
  `DailyBar.open`이다.
- Long entry price는 local `Context(prec=28, rounding=ROUND_HALF_EVEN)`에서
  `open * (1 + slippage)`로 계산하며 intermediate rounding을 하지 않는다.
- FeeModel decimal 값은 durable internal request payload에서 문자열로 보존한다.
- Commission cashflow와 sell-side slippage는 trade lifecycle 단계에서 적용한다.
