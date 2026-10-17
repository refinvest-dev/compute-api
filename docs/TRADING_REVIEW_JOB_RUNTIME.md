# Trading Review Normalization Job Runtime

This runtime implements the internal normalization subset of Context's Trading Review API and
OpenAPI 0.4.0 (ADR-064). Core is the sole caller. The FastAPI process accepts and polls jobs; the separate
`refinvest-trading-review-worker` process downloads two short-lived Core artifact grants and calls
`refinvest.trading_review.service.normalize_and_reconstruct` in an isolated child process.

## Deployment

Apply migrations through `0014_lock_trading_normalization_result_integrity.sql`. Install the
Trading Review worker and cleanup systemd units in `deploy/systemd/`. Configure the worker with
the same PostgreSQL URL as the API, stable identity and masking secrets, the pinned timezone
database version, and an explicit comma-separated `host:port` allowlist for Core artifact grant
URLs. Secrets and signed URLs must not be placed in logs or metrics. Run one worker initially;
multiple workers use PostgreSQL `FOR UPDATE SKIP LOCKED` claims and lease-owner CAS.
API and worker processes must share `REFINVEST_TRADING_REVIEW_PAYLOAD_KEY`, a URL-safe
base64-encoded 32-byte AES-GCM key from secret management. The cleanup process does not need it.
Keep the previous key available to both processes until all payloads it encrypted have expired
and been cleaned up; an uncoordinated key change makes pending payloads unreadable.
Trading Review uses a separately configured bounded active-job pool, independent of the
Backtest pool. API and worker must use the same configured capacity, grant renewal cutoff,
wait window, and retry budget. New admissions over capacity return `503` with `Retry-After`;
idempotent replays and grant updates do not reserve another slot.
Each claim registers two attempt-scoped `ArtifactLease` rows with the Core artifact role, hash,
grant expiry, and deletion generation. Reclaimed attempts expire their leases; terminal attempts
close them. Signed grants are accepted for at most five minutes and downloaded only from the
configured host allowlist.

The worker uses a 60-second lease, 15-second heartbeat, 10-minute execution deadline, and three
attempts by default. An expired lease closes its attempt and requests `START_RETRY_ATTEMPT`;
the consumed grant is never reclaimed automatically. The current lease owner, attempt number,
Core attempt token, grant revision, and deletion generation gate publication. Cancellation is
best effort and wins while a running
attempt holds its publication lock. The calculation happens outside PostgreSQL transactions.

## Core handoff and retention

Core supplies a stable 1–128 character `Idempotency-Key` per logical dispatch. The canonical
fingerprint includes semantic request fields, artifact hashes, and the initial attempt token;
it excludes short-lived signed grant URLs and expiry timestamps. A same-key replay returns the
original job without changing grants, even after expiry. Core uses a separate idempotent
`grant-updates` operation to replace queued grants with the same token or start a retry with a
new token. Both modes require current revision/token CAS, fresh role-pinned grants, and an
unexpired DB-clock deadline. The admission cutoff is fixed at creation; the bounded wait
deadline never extends on duplicate polling or update replay. A changed semantic request gets
`409`. An independent partial unique index prevents two active jobs for one Core logical ID.

`GET /trading-review/normalization-jobs/{jobId}/result` returns a descriptor only after
`COMPLETED`; Core downloads the opaque payload, verifies size and SHA-256, stores it durably, and
then acknowledges with final token/generation and either `PERSISTED` or `DISCARDED_STALE`.
Acknowledgement is idempotent, including after acknowledged payload-byte cleanup
while its receipt remains. The job/descriptor metadata remains until terminal+24 hours;
completed payload bytes can be purged after acknowledgement. Failed and cancelled
jobs are deleted after 24 hours. Cleanup uses PostgreSQL time and a bounded batch, and never
deletes active jobs. The cleanup timer runs hourly. Core must not treat a later `404` as a new
logical dispatch.

The terminal payload follows OpenAPI's canonical record and evidence schemas. Public DTOs are
ordered and canonicalized before V1 count, nine section digests, reconciliation manifest hash,
and overall resultHash are calculated. `payloadSha256` covers the exact uncompressed UTF-8
HTTP entity body and is independent of those semantic hashes. The V1 transport is one resource;
section and manifest SHA-256 inputs are streamed in canonical chunks rather than copied as
whole section byte arrays. Page/chunk retrieval requires a future schema version. Counts,
digests, and hash versions are
stored with encrypted bytes in the same terminal transaction, and a database trigger prevents
later integrity-metadata edits. Legacy rows without V1 digests are not reinterpreted.
The payload contains
allowlisted trading values needed by Core and excludes source UID, full Trade ID/Order ID, raw
CSV bytes, filenames, signed grant URLs, API keys, and stack traces. Payload bytes are encrypted
at rest using AES-256-GCM with a per-payload nonce and authenticated payload ID; descriptors
retain the SHA-256 and byte size of the plaintext that Core receives. Exclude this short-lived
Compute storage from long-lived backups. Metrics use fixed status and safe failure categories only.

## Verification

Set `REFINVEST_TEST_DATABASE_URL` to an isolated PostgreSQL database with migration 0014 applied,
then run `uv run pytest tests/integration/test_postgres_trading_review_jobs.py`. Route and worker
tests use synthetic artifacts. Integration fixture validation is a separate sibling-checkout
contract check; it is not a Compute runtime dependency. Core dispatch and HTTP end-to-end tests
belong to the follow-up Core implementation.
