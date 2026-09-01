# Backtest Worker Runbook

The durable worker runs separately from the Compute API process and consumes only
PostgreSQL jobs that have already pinned an immutable DatasetSnapshot. Its lifecycle,
lease, retry, and terminal-failure behavior are defined by ADR-018.

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

The snapshot timer and worker service have separate environment files and enablement:

| Unit | Role | Enablement |
|---|---|---|
| `refinvest-snapshot-ingestion.timer` | Publishes one immutable daily snapshot | One designated Compute EC2 |
| `refinvest-backtest-worker.service` | Claims and executes durable backtest jobs | One worker initially |

Increasing worker count is a capacity decision. Although PostgreSQL leases prevent two
workers from claiming the same job, a multi-worker deployment requires its own throughput
and database-capacity review.
