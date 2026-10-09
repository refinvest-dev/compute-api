# Trading Review Normalization Job Runtime

This runtime implements the internal normalization subset of Context's Trading Review API and
OpenAPI 0.3.0. Core is the sole caller. The FastAPI process accepts and polls jobs; the separate
`refinvest-trading-review-worker` process downloads two short-lived Core artifact grants and calls
`refinvest.trading_review.service.normalize_and_reconstruct` in an isolated child process.

## Deployment

Apply migrations through `0010_create_trading_review_normalization_jobs.sql`. Install the
Trading Review worker and cleanup systemd units in `deploy/systemd/`. Configure the worker with
the same PostgreSQL URL as the API, stable identity and masking secrets, the pinned timezone
database version, and an explicit comma-separated `host:port` allowlist for Core artifact grant
URLs. Secrets and signed URLs must not be placed in logs or metrics. Run one worker initially;
multiple workers use PostgreSQL `FOR UPDATE SKIP LOCKED` claims and lease-owner CAS.
API and worker processes must share `REFINVEST_TRADING_REVIEW_PAYLOAD_KEY`, a URL-safe
base64-encoded 32-byte AES-GCM key from secret management. The cleanup process does not need it.
Keep the previous key available to both processes until all payloads it encrypted have expired
and been cleaned up; an uncoordinated key change makes pending payloads unreadable.
Context/OpenAPI does not define a numeric normalization admission capacity or a shared
Backtest capacity pool, so this runtime does not impose a new numeric limit.
Each claim registers two attempt-scoped `ArtifactLease` rows with the Core artifact role, hash,
grant expiry, and deletion generation. Reclaimed attempts expire their leases; terminal attempts
close them. Signed grants are accepted for at most five minutes and downloaded only from the
configured host allowlist.

The worker uses a 60-second lease, 15-second heartbeat, 10-minute execution deadline, and three
attempts by default. Retryable failures wait 30 seconds and then two minutes. A crashed worker's
expired lease can be reclaimed; only the current lease owner, attempt number, Core attempt token,
and deletion generation can finish a job. Cancellation is best effort and wins while a running
attempt holds its publication lock. The calculation happens outside PostgreSQL transactions.

## Core handoff and retention

Core supplies a stable 1–128 character `Idempotency-Key` per logical dispatch. The canonical
fingerprint includes semantic request fields and artifact hashes; it excludes short-lived signed
grant URLs and expiry timestamps. A same-key replay returns the original job, and fresher grants
can replace expired grants while a job is queued or waiting to retry. A changed semantic request
gets `409`. An independent partial unique index prevents two active jobs for one Core logical ID.

`GET /trading-review/normalization-jobs/{jobId}/result` returns a descriptor only after
`COMPLETED`; Core downloads the opaque payload, verifies size and SHA-256, stores it durably, and
then acknowledges. Acknowledgement is idempotent, including after acknowledged payload cleanup
while its 24-hour receipt remains. Completed payloads expire after 24 hours; failed and cancelled
jobs are deleted after 24 hours. Cleanup uses PostgreSQL time and a bounded batch, and never
deletes active jobs. The cleanup timer runs hourly. Core must not treat a later `404` as a new
logical dispatch.

The terminal payload follows OpenAPI's canonical record and evidence schemas. It contains
allowlisted trading values needed by Core and excludes source UID, full Trade ID/Order ID, raw
CSV bytes, filenames, signed grant URLs, API keys, and stack traces. Payload bytes are encrypted
at rest using AES-256-GCM with a per-payload nonce and authenticated payload ID; descriptors
retain the SHA-256 and byte size of the plaintext that Core receives. Exclude this short-lived
Compute storage from long-lived backups. Metrics use fixed status and safe failure categories only.

## Verification

Set `REFINVEST_TEST_DATABASE_URL` to an isolated PostgreSQL database with migration 0010 applied,
then run `uv run pytest tests/integration/test_postgres_trading_review_jobs.py`. Route and worker
tests use synthetic artifacts. Integration fixture validation is a separate sibling-checkout
contract check; it is not a Compute runtime dependency. Core dispatch and HTTP end-to-end tests
belong to the follow-up Core implementation.
