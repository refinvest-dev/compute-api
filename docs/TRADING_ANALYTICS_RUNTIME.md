# Trading Review Analytics Job Runtime

Compute exposes the internal Analytics Job Protocol V1 from the Context repository. Core owns the immutable LedgerRevision and supplies two short lived, read only artifact grants. Compute does not query the Core database. Core `TradingAnalysisRun` orchestration, durable result publication, and public Metric APIs are outside this runtime.

## Start

Apply additive migration `migrations/0016_create_trading_analytics_jobs.sql` after 0015. Install the project so the console scripts are available. Start the API with the existing `refinvest` command and the independent worker with `refinvest-trading-analytics-worker`. Run `refinvest-cleanup-trading-analytics-jobs` hourly. The matching systemd service, timer, and environment examples are under `deploy/systemd`.

The API and worker require `REFINVEST_DATABASE_URL`, `REFINVEST_TRADING_REVIEW_ACTIVE_JOB_LIMIT`, `REFINVEST_TRADING_REVIEW_PAYLOAD_KEY`, and `REFINVEST_TRADING_ANALYTICS_IMPLEMENTATION_DIGEST`. The API also requires `REFINVEST_INTERNAL_API_KEY`. The worker requires `REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS`, an allowlist of Core artifact host and port pairs. `REFINVEST_TRADING_ANALYTICS_MAX_INPUT_BYTES` bounds each downloaded input artifact (default 512 MiB). `REFINVEST_TRADING_REVIEW_MAX_PAYLOAD_BYTES` bounds the encrypted terminal resource; the job's pinned delivery limit may be lower. The worker ID and poll interval use the existing `REFINVEST_WORKER_ID` and `REFINVEST_WORKER_POLL_SECONDS` settings. Secrets and signed locators must come from deployment secret management and Core, respectively.

## Job lifecycle

Core calls `POST /trading-review/analytics-jobs` with an `Idempotency-Key`. An identical replay returns the current job status; reuse with changed semantic pins is a conflict. A new job starts `PENDING`. The worker claims it with `FOR UPDATE SKIP LOCKED`, advances it to `RUNNING`, and renews its 60 second lease every 20 seconds. A process restart can reclaim an expired lease on the same attempt. Publication compares attempt token, deletion generation, lease owner, and lease generation before atomically storing a `COMPLETED` result and descriptor. Terminal states are `COMPLETED`, `FAILED`, and `CANCELLED` and cannot run again.

The worker verifies each artifact's exact byte length and SHA-256 before parsing. It then validates the LedgerRevision content and reconciliation manifest identities, hashes, capability snapshot, AnalysisConfig, and version pins. It adapts verified records and ReviewUnits to the existing pure Behavior Metric Engine and runs all three V1 metrics. A missing capability or insufficient sample is a completed Metric status. Invalid input and calculation failure retain separate safe codes; neither publishes a partial result.

Retryable attempt failure returns the job to `PENDING` with `retryAvailable=true`; Core must send `START_RETRY_ATTEMPT` with fresh grants and a new attempt token. There are at most three attempts within 24 hours of create. `REPLACE_QUEUED_GRANTS` refreshes only an unclaimed pending attempt. Core may request cancellation. A lost lease, old attempt, or cancelled job cannot publish. Compute has no live Core deletion generation lookup; Core applies current deletion and attempt checks before publication and sends `DISCARDED_STALE` for an available stale completed result.

Core polls `GET /trading-review/analytics-jobs/{jobId}`, obtains the Analytics specific descriptor from `GET /trading-review/analytics-jobs/{jobId}/result`, then downloads `/trading-review/terminal-payloads/{payloadId}`. The descriptor advertises exact byte size, transport SHA-256, logical result hash, six section digests, evidence hash, and identity pins. The payload is UTF-8 without BOM using identity content encoding. Core verifies and commits its result before sending `POST /trading-review/jobs/{jobId}/acknowledgement`. Identical ACK replay is safe; changed ACK fields conflict. The resource bytes are removed after ACK. The cleanup command purges expired bytes and terminal job metadata by 24 hours after terminal state.

## Verification

Contract ownership and the AR-02A compatibility checks are documented in
[Analytics contracts](ANALYTICS_CONTRACTS.md). Engine-to-public projection and verified input adaptation now live in application; identity and digest
construction live in `contracts/analytics`. Worker coordination, Artifact HTTP adapter boundaries and
AR-05A verification are described in [Analytics worker](ANALYTICS_WORKER_APPLICATION.md).
The app worker module retains configuration, composition, signal handling and the CLI entrypoint.

The current application worker fetches, validates, calculates, projects and serializes in the
parent process while a separate thread renews its lease. It does not have an enforceable
attempt-wide deadline or child process memory isolation. Download and payload byte caps
are not RSS limits. ADR-071 remains DRAFT; see the
[execution policy review](ANALYTICS_EXECUTION_POLICY_REVIEW.md)
and [final architecture audit](ARCHITECTURE_FINAL_AUDIT.md). Backtest/Normalization process
isolation and synthetic Analytics vectors do not establish actual Core Analytics runtime E2E.

Job orchestration, application-owned errors, repository ports and the database-backed HTTP/worker
smoke are described in [Analytics application](ANALYTICS_APPLICATION.md). Providers live in
`api/dependencies/trading_analytics_jobs.py`. Shared payload/ACK routing delegates to the
[terminal delivery application](TERMINAL_DELIVERY_APPLICATION.md).

Run `python -m pytest -q tests/test_trading_analytics_runtime.py` for fixed vector Engine and API delivery coverage. Set `REFINVEST_TEST_DATABASE_URL` to an isolated PostgreSQL database with migrations 0010–0016 applied, then run `python -m pytest -q tests/integration/test_postgres_trading_analytics_jobs.py`. The test database must be separate from production. Run the full Compute suite and `integration/scripts/verify-contract-sync.ps1 -IncludeWeb` from the sibling Integration checkout before release.
