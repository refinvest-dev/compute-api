# Snapshot Ingestion Runbook

CLI 입력·coverage/override 검증·ID 생성과 발행 lifecycle은 `ingestion/snapshot/cli.py`,
설정·clock/nonce·vendor/DB/storage 조립과 console 진입점은 `app/snapshot_ingestion.py`가 소유한다.
기존 journal logger 이름과 이벤트, public Python 진입점·옵션·exit 동작은 유지한다.
이동 범위와 검증 증적은 [AR-11F](INGESTION_CLI_BOUNDARY.md)에 기록한다.

Run the external job once per day after 00:15 UTC. The MVP production scheduler is the
paired `systemd` service and timer under `deploy/systemd/`; see ADR-038. Do not retry by
overwriting an existing snapshot directory.

Required environment variables:

```text
REFINVEST_DATABASE_URL
REFINVEST_MASSIVE_API_KEY
REFINVEST_SNAPSHOT_STORAGE_BACKEND=s3
REFINVEST_SNAPSHOT_STORAGE_S3_BUCKET=refinvest-snapshots
REFINVEST_SNAPSHOT_STORAGE_S3_PREFIX=daily
REFINVEST_SNAPSHOT_STORAGE_S3_REGION=ap-northeast-2
REFINVEST_SNAPSHOT_START_DATE=2010-01-01
REFINVEST_US_EQUITY_OVERRIDES_PATH=/run/refinvest/us-equity-overrides.json
```

S3 production access uses the Compute EC2 instance role; never place AWS access keys in
the environment file. `REFINVEST_SNAPSHOT_STORAGE_ROOT` is only required when the
explicit local-development backend `REFINVEST_SNAPSHOT_STORAGE_BACKEND=filesystem` is
selected. ADR-039 defines the S3 object and immutability contract.

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

## Published reference-data smoke check

After a production publication, run this command on the Compute host with the same database and
S3 instance-role configuration as the API and worker:

```text
refinvest-verify-reference-data
```

진단 조율은 `application/reference_data_diagnostics.py`, 설정·DB/storage 조립과 CLI는
`app/reference_data_smoke.py`가 맡는다. 보존 범위는 [AR-11G](REFERENCE_DATA_DIAGNOSTICS_BOUNDARY.md)에 기록한다.

It resolves the latest published DatasetSnapshot once (or a supplied
`--dataset-snapshot-id`), manifest-verifies its artifact, confirms calendar availability for all
six MVP assets, and reads a recent QQQ/BTCUSDT cross-calendar PRICE series. It prints selected
snapshot provenance as JSON on success and exits non-zero without a traceback on any failure. It
does not publish, modify, or fall back to another snapshot.

## EC2 systemd deployment

Install `refinvest-snapshot-ingestion.service` and
`refinvest-snapshot-ingestion.timer` on the existing always-on Compute EC2. Adjust the
repository and virtual-environment paths in the service template only if the deployment
path differs from `/opt/refinvest/compute-api`.

1. Create the `refinvest` service account and attach an EC2 instance role that has
   least-privilege access to the configured S3 bucket/prefix.
2. Copy `deploy/systemd/snapshot-ingestion.env.example` to
   `/etc/refinvest/snapshot-ingestion.env`, replace its placeholders from the approved
   secret store, and set its owner to `root:root` and mode to `0600`.
3. Create `/etc/refinvest/us-equity-overrides.json` with the immutable JSON content for
   this deployment. Make it root-owned and readable by the `refinvest` service account;
   do not place secrets in this file.
4. Copy the two unit files to `/etc/systemd/system/`, run `systemctl daemon-reload`, then
   run `systemctl enable --now refinvest-snapshot-ingestion.timer`.
5. Verify `systemctl list-timers refinvest-snapshot-ingestion.timer` shows the next
   00:20 UTC run. Inspect executions with
   `journalctl -u refinvest-snapshot-ingestion.service`.

Enable the timer on exactly one Compute EC2 instance. A standby instance may retain the
files but must leave the timer disabled. Configure the platform monitoring route to alert
when it receives `snapshot_publication_failed` or no `snapshot_publication_completed` event for
the scheduled daily run; the unit's 15-minute retry is not an alert substitute. Events are JSON
journal records and include the snapshot ID, coverage, storage path, and terminal duration. A
failure event includes only the exception type, never an exception message or credential. See
ADR-047.

The durable backtest worker is a separate long-running service. Its systemd deployment
and stop/recovery procedure are documented in `docs/BACKTEST_WORKER.md`.
