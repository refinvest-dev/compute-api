# ADR-039 — S3 Snapshot Artifact Storage

## Status

Accepted for the Daily Bar MVP.

## Decision

- New production DatasetSnapshot artifacts use Amazon S3. Their immutable
  `storage_path` is `s3://<bucket>/<normalized-prefix>/<snapshot-id>`.
- S3 object keys and bytes use the exact ADR-033 artifact contract. Calendar and market
  data objects are conditionally created first; `manifest.json` is conditionally created
  last. `If-None-Match: *` prevents overwriting an existing key.
- S3 publication failure leaves no new DatasetSnapshot metadata. Partial, unreferenced
  objects are retained rather than deleted or reused; the next scheduled run has a new
  snapshot ID under ADR-037.
- Compute obtains S3 access through the AWS SDK default credential chain. On EC2 this
  is an instance role with least-privilege access to the configured bucket/prefix. Access
  keys are not stored in systemd environment files.
- The worker dispatches from pinned `storage_path`: historical filesystem artifacts
  remain readable, while `s3://` artifacts use the S3 loader. Both readers verify the
  same manifest and SHA-256 values before exposing market data to the Engine.

## Consequences

- Reproducible historical snapshots survive replacement of the Compute EC2 instance.
- The S3 bucket needs versioning/lifecycle and IAM policy management in infrastructure.
  These controls must preserve every object referenced by retained DatasetSnapshot
  metadata; lifecycle deletion is not an application fallback mechanism.
- The filesystem publisher remains available only for local development and existing
  historical artifacts. New production scheduler configuration selects `s3` explicitly.
