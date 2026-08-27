# ADR-020 — Temporal Session Navigation

## Status

Accepted for the Daily Bar MVP.

## Decision

- `lag` and `holdingSignalSessions` navigate the explicit `EXPECTED` session sequence of the Primary Signal Asset only. Offset zero returns the signal session itself.
- Entry and exit execution resolve to the first `EXPECTED` session of the Execution Asset strictly after the signal-derived anchor session.
- An anchor that is absent from the Primary Signal Asset sequence, an offset beyond available sessions, or no later Execution Asset session is `CALENDAR_RESOLUTION_FAILED`.
- Temporal navigation reads only the pinned DatasetSnapshot calendar artifact. It does not infer dates from weekdays, call a holiday API, or substitute a neighboring session.
- Cross-calendar metric anchoring is intentionally deferred to the metric evaluation slice.

## Consequences

- BTC weekend signals can resolve to the next US equity session without special-case logic.
- The same snapshot calendar always produces the same signal and execution sessions.
