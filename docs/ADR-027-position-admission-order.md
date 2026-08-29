# ADR-027 — Single Active Position Admission Order

## Status

Accepted for the Daily Bar MVP.

## Decision

- Resolve entry and exit execution sessions before price lookup.
- Process candidates by `(entry_execution_session, signal_session)`.
- A position remains active through its exit execution session. A candidate whose
  entry session is on or before that session is ignored.
- Ignored candidates do not cause entry or exit Daily Bar reads.
- Only accepted positions proceed to execution-price lookup.
