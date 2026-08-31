# Snapshot Ingestion Runbook

Run the external job once per day after 00:15 UTC. Configure retries and alerting in
the scheduler; do not retry by overwriting an existing snapshot directory.

Required environment variables:

```text
REFINVEST_DATABASE_URL
REFINVEST_MASSIVE_API_KEY
REFINVEST_SNAPSHOT_STORAGE_ROOT
REFINVEST_SNAPSHOT_START_DATE=2010-01-01
REFINVEST_US_EQUITY_OVERRIDES_PATH=/run/refinvest/us-equity-overrides.json
```

The override file is an immutable JSON object. `null` means a closure; a timestamp
means an early close and must be exact UTC.

```json
{
  "2025-01-09": null,
  "2025-11-28": "2025-11-28T18:00:00Z"
}
```

Normal scheduled invocation needs no arguments:

```text
refinvest-publish-snapshot
```

It publishes `[REFINVEST_SNAPSHOT_START_DATE, yesterday UTC]` and prints JSON with the
new snapshot ID and storage path. For a controlled backfill, provide explicit
`--start`, `--end`, `--snapshot-id`, and optionally `--us-equity-overrides`; an end on
the current/future UTC date is rejected.
