# Backtest Worker Runbook

The durable worker runs separately from the Compute API process and consumes only
PostgreSQL jobs that have already pinned an immutable DatasetSnapshot. Its lifecycle,
lease, retry, and terminal-failure behavior are defined by ADR-018.

`app/worker.py` composes the application host, repository and process adapter. The parent
owns leases and conditional terminal publication; the spawned child owns calculation.
See [process adapter](BACKTEST_PROCESS_ADAPTER.md) and [current architecture](ARCHITECTURE.md).
Analytics currently has a different execution boundary; this Backtest deadline does not define
an Analytics deadline or memory limit.

## EC2 systemd deployment

Install `deploy/systemd/refinvest-backtest-worker.service` on the existing always-on
Compute EC2. The template expects the repository and virtual environment at
`/opt/refinvest/compute-api`; adjust both paths together if the deployment layout differs.

1. Attach the EC2 instance role that can read the S3 artifact prefixes referenced by
   DatasetSnapshot metadata. Do not configure AWS access keys in the environment file.
2. Copy `deploy/systemd/backtest-worker.env.example` to
   `/etc/refinvest/backtest-worker.env`, provide the database URL and deployed engine
   version, then set owner `root:root` and mode `0600`.
3. Copy the unit file to `/etc/systemd/system/` and run `systemctl daemon-reload`.
4. Enable exactly one worker initially with
   `systemctl enable --now refinvest-backtest-worker.service`.
5. Confirm steady state with `systemctl status refinvest-backtest-worker.service` and
   inspect failures with `journalctl -u refinvest-backtest-worker.service`.

The worker receives `SIGTERM` during a normal service stop. It stops claiming new jobs;
an in-flight job either completes before `TimeoutStopSec` or is recovered from its expired
lease under ADR-018. `Restart=on-failure` restores the worker process after host-level or
unexpected process failures. This does not alter the application-level retry schedule for
an individual backtest.

Each claimed attempt runs in an isolated child process with a 10-minute execution deadline.
The parent continues the PostgreSQL lease heartbeat, terminates a timed-out child, and
records `FAILED` with `EXECUTION_TIMEOUT`; it does not retry that job automatically. Set
`REFINVEST_BACKTEST_EXECUTION_TIMEOUT_SECONDS` only to a positive value approved by the
operational policy in ADR-042.

## Operational observability

Worker and cleanup services emit JSON events to their systemd journals under ADR-043. Forward
those existing journals to CloudWatch Logs (or the deployment-owned equivalent); do not add
AWS credentials or a metrics SDK to this service. Configure alerts for the following events
or service conditions:

| Signal | Warning threshold | Required response |
|---|---:|---|
| `backtest_queue_wait_threshold_exceeded` | 10 minutes | Check worker availability and queue growth. |
| `backtest_execution_duration_threshold_exceeded` | 5 minutes | Inspect engine/snapshot cost before timeout. |
| `backtest_execution_failed` with `EXECUTION_TIMEOUT` | Immediate | Inspect run, snapshot, and engine version. |
| `backtest_retry_exhausted` | Immediate | Investigate infrastructure or execution failure. |
| `backtest_stale_lease_reclaimed` | Immediate | Inspect prior worker termination or heartbeat loss. |
| cleanup unit failure / overdue terminal backlog | Immediate | Restore cleanup and verify ADR-041 retention. |

Every durable `FAILED` transition also emits `backtest_execution_failed` with
`run_status=FAILED` and a normalized `error_code`, followed by a log-derived counter increment:
`metric_name=backtest_runs_failed_total`, `metric_type=counter`, `value=1`, and the sole
low-cardinality tag `error_code`. `run_id` and `snapshot_id` appear only on the failure event,
never on the counter. A missing or unrecognized legacy code is counted as
`error_code=UNSPECIFIED`. Failure reasons, request payloads, and exception traces are omitted
from both events.

`backtest_cleanup_completed` includes `deleted_count` for normal hourly audit. Log forwarding,
alert destinations, and log retention are infrastructure-owned configuration.

The snapshot timer and worker service have separate environment files and enablement:

| Unit | Role | Enablement |
|---|---|---|
| `refinvest-snapshot-ingestion.timer` | Publishes one immutable daily snapshot | One designated Compute EC2 |
| `refinvest-backtest-worker.service` | Claims and executes durable backtest jobs | One worker initially |
| `refinvest-backtest-job-cleanup.timer` | Removes expired terminal runtime jobs | One designated Compute EC2 |

Increasing worker count is a capacity decision. Although PostgreSQL leases prevent two
workers from claiming the same job, a multi-worker deployment requires its own throughput
and database-capacity review.

The Core-facing FastAPI process is a third, separate unit. Its internal authentication,
private-network, and TLS deployment requirements are documented in `docs/COMPUTE_API.md`.

## Terminal job cleanup

`refinvest-backtest-job-cleanup.timer` runs once per hour on the same designated Compute
EC2. It invokes a bounded PostgreSQL cleanup batch for terminal jobs at least 24 hours old;
it never deletes queued, running, or retrying work. Install the paired service and copy
`deploy/systemd/backtest-job-cleanup.env.example` to
`/etc/refinvest/backtest-job-cleanup.env` with mode `0600`, then run:

```text
systemctl daemon-reload
systemctl enable --now refinvest-backtest-job-cleanup.timer
systemctl list-timers refinvest-backtest-job-cleanup.timer
```

Monitor the service journal for the JSON event `backtest_cleanup_completed` and its
`deleted_count`. Core must persist any
terminal result before the 24-hour retention horizon; a later `404` is not permission to
submit the same logical request again.
