# ADR-040 — Backtest Submission Idempotency

## Status

Accepted for coordinated Core and Compute rollout.

## Context

`POST /backtests` creates a durable Compute job. A Core timeout or lost response can
therefore leave Core unable to distinguish a successfully accepted request from one that
never reached Compute. Retrying the current contract creates a separate job, which can
produce duplicate computation and duplicate polling state.

ADR-018 deliberately prohibits implicit request deduplication until this external contract
is explicit.

## Proposed Decision

- Core generates one opaque UUID `Idempotency-Key` for each logical backtest submission
  and preserves that exact key for every transport retry. A new user-initiated submission
  receives a new key.
- `POST /backtests` requires the header after coordinated rollout. Compute stores the
  key, a canonical request fingerprint, the generated `runId`, and the initial accepted
  response atomically with the durable job.
- A repeated key with the same canonical request returns the original `202` acceptance
  response, including the same `runId`. Core then continues normal status polling; a
  replay does not create or restart a job.
- A repeated key with a different canonical request returns `409 Conflict`. Compute never
  chooses one payload, merges payloads, or silently creates a second job.
- The idempotency columns remain available for the same 24-hour retention period as
  terminal jobs. Core retry horizon must remain shorter than 24 hours; an older retry
  uses a newly generated key.

## Alternatives Considered

### `requestId` in the JSON body

This ties request transport identity to the domain payload and requires every stored
request representation to carry an otherwise non-calculation field. A dedicated HTTP
header better expresses retry identity and leaves `StrategyVersion` / Engine inputs
unchanged.

### Implicit deduplication from request content

Identical strategies can be intentionally submitted more than once, and canonicalization
rules would become an unrequested source of product behavior. Compute must deduplicate
only when Core explicitly supplies a key.

## Rollout Requirements

1. Core generates and persists a UUID v4 per logical submission, reusing it only for
   retries within 24 hours.
2. Core continues polling the returned `runId`; replay responses intentionally retain the
   original `202/PENDING` acceptance shape even if the Job has since advanced.
3. Compute applies the database migration before accepting the new required header.

## Determinism / Point-in-Time

- Determinism impact: none — idempotency selects an existing durable request; it does not
  alter a request's pinned DatasetSnapshot, Engine Version, or calculation inputs.
- Point-in-Time impact: none — no calendar, market data, or execution-time rule changes.
