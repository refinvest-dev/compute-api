# Compute API Runbook

Trading Review normalization jobs are documented in
`docs/TRADING_REVIEW_JOB_RUNTIME.md`. They use the same internal API key boundary and a
separate worker/retention lifecycle from Backtest jobs.

The Compute API accepts Core-only backtest submissions and status polling. Backtest
routes require `X-Internal-Api-Key`; `/health` is intentionally unauthenticated for a
private deployment probe.

## Backtest submission idempotency

Core includes a new UUID v4 `Idempotency-Key` on every logical `POST /backtests`
submission and retains the same value for a retry within 24 hours. A new user submission
uses a new key. Compute returns the original `202` acceptance and `runId` for the same
key and canonical request, while a reused key with another request returns `409`.
Core always polls the returned `runId` for current status. See ADR-040.

## Asset reference data

`GET /assets` and `GET /assets/{symbol}/availability` are Core-only reference endpoints.
Compute owns the six-item static registry and returns `inceptionDate`, calendar, and execution
eligibility. Availability comes from one latest published immutable DatasetSnapshot and includes
that snapshot ID and creation time. A supported asset without current snapshot data returns
`503 DATASET_UNAVAILABLE`; Core must not infer availability from an external vendor. See ADR-045.

## Snapshot series data

`GET /series` is a Core-only Data Explorer endpoint. It resolves one immutable snapshot at
request start (or the requested `datasetSnapshotId`) and returns its ID, creation time, and
adjustment policy with the points. `PRICE` is close, `RETURN` is the prior available session's
daily return, and `NORMALIZED` sets the first available close in the requested range to 100.
Assets retain their own trading sessions: no common-date alignment, non-trading fill, or missing
expected-session fallback is allowed. A missing expected close or malformed artifact fails with
`503 DATASET_CORRUPTION`; no available snapshot is `503 DATASET_UNAVAILABLE`. Requests allow 1–6
unique symbols, up to ten years and 20,000 returned points. See ADR-046.

## Backtest runtime retention

Compute retains terminal job status, result/failure payload, and idempotency data for 24
hours after terminal transition. Core must copy terminal results into its long-term store
within that period. Afterwards `GET /backtests/{runId}` returns `404`, which means either
an unknown run or an expired Compute runtime record; it does not authorize re-submission of
the same logical request. See ADR-041.

## Backtest execution deadline

One snapshot-pinned execution attempt has a 10-minute wall-clock limit. A limit breach is
terminal `FAILED` with `errorCode: EXECUTION_TIMEOUT`; Compute does not retry it
automatically. Core may surface the failure and create a distinct, new user submission when
appropriate. See ADR-042.

## Worker operational signals

Worker queue and execution timing are emitted as JSON systemd journal events under ADR-043.
They are deployment observability signals only: they do not change polling responses,
calculation semantics, or job scheduling. Infrastructure forwards them to its approved log
sink and alerts on warning events.

## Backtest admission capacity

Compute accepts at most 20 `PENDING` or `RUNNING` jobs globally. A new request when full
returns `503` with `errorCode: BACKTEST_CAPACITY_EXCEEDED` and `Retry-After: 30`; it creates
no job or idempotency record. Core retries the same logical request with the same
`Idempotency-Key` only after that delay. Existing idempotent replays always return the
original acceptance even while capacity is full. See ADR-044.

## EC2 systemd deployment

Install `deploy/systemd/refinvest-compute-api.service` on the existing always-on Compute
EC2. The template runs Uvicorn without development reload mode and expects the repository
and virtual environment at `/opt/refinvest/compute-api`.

1. Copy `deploy/systemd/compute-api.env.example` to
   `/etc/refinvest/compute-api.env`, replace both placeholders from the approved secret
   store, and set owner `root:root` and mode `0600`.
2. Copy the unit file to `/etc/systemd/system/` and run `systemctl daemon-reload`.
3. Enable the API with `systemctl enable --now refinvest-compute-api.service`.
4. From the private host or approved internal monitor, verify
   `curl --fail http://127.0.0.1:8000/health`.
5. Inspect lifecycle and application errors with
   `journalctl -u refinvest-compute-api.service`.

`Restart=on-failure` restarts an unexpectedly exited Uvicorn process after 15 seconds.
`SIGTERM` permits a normal Uvicorn shutdown; the API itself holds no running backtest
state, because durable jobs and their leases belong to PostgreSQL and the worker.

## Internal network and TLS boundary

The systemd unit binds port 8000 for a private network listener. Infrastructure must
enforce all of the following before allowing Core traffic:

- Security groups or equivalent firewall rules permit port 8000 only from Core or an
  internal load balancer; never expose Compute directly to the public internet.
- TLS terminates at an internal load balancer or reverse proxy. Its certificate and
  listener configuration are infrastructure-managed and are not stored in this repo.
- Core sends the configured `X-Internal-Api-Key` on every `/backtests` request and poll.
  It also supplies the required `Idempotency-Key` on every submission. Network isolation
  and TLS complement, rather than replace, these application checks.
- Health probing stays within the private network because `/health` does not require the
  internal API key.

The API service, worker service, and snapshot timer can coexist on the initial Compute
EC2 but have separate unit files and EnvironmentFiles. Restarting the API does not stop
the worker or alter a pinned DatasetSnapshot.
