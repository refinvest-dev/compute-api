# ADR-038 — EC2 Snapshot Ingestion Scheduling

## Status

Accepted for the Daily Bar MVP.

## Decision

- The existing always-on Compute EC2 instance runs the external snapshot scheduler. The
  MVP does not add a separate always-on ingestion host or an in-process scheduler.
- A `systemd` `Type=exec` service invokes `refinvest-publish-snapshot`; its paired
  timer runs at 00:20 UTC every day. `Persistent=true` runs a missed schedule after a
  host restart.
- Exactly one Compute EC2 instance enables the timer. A replacement or disaster-recovery
  instance keeps the unit installed but disabled until it becomes the designated
  scheduler owner.
- The service retries failures through `Restart=on-failure` with a 15-minute delay and
  a bounded start-rate limit. Platform monitoring must alert on a failed service or a
  timer that has not produced a successful daily run. Alert destinations are deployment
  configuration, not Compute application configuration.
- Secrets are supplied only through a root-owned `EnvironmentFile` with mode `0600`.
  Unit files, repository files, and journals must not contain database credentials,
  vendor API keys, or internal API keys. The immutable US-equity override file is
  separately mounted read-only.
- Production publication uses the S3 storage backend defined by ADR-039. The snapshot
  CLI continues to create a new immutable object prefix for every attempt and never
  replaces a prior artifact.

## Consequences

- The existing EC2's fixed cost is reused and there is no additional scheduler control
  plane cost for the MVP.
- A daily job may be delayed by up to the retry interval after transient vendor or
  infrastructure errors, but it never silently publishes partial or substituted data.
- Deployment must install the units, provision the environment and override files, and
  enable the timer on exactly one host. Actual EC2 access, service enablement, and
  alert-route configuration remain infrastructure operations.
