# ADR-026 — Holding Period Anchor

## Status

Accepted for the Daily Bar MVP.

## Decision

- `holdingSignalSessions` starts at `EntryCandidate.entry_signal_session`, the
  Primary Signal Asset session after lag has been applied.
- The exit execution session is the first expected Execution Asset session
  strictly after that holding-derived signal anchor.
- Exit reference price is that execution session's `DailyBar.open`; long sell
  price is `open * (1 - slippage)` under ADR-025's Decimal context.
