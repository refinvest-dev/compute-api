-- ADR-064: immutable creation, separately idempotent grant revisions, and bounded waits.
ALTER TABLE trading_review_jobs
    ADD COLUMN grant_revision BIGINT NOT NULL DEFAULT 0 CHECK (grant_revision >= 0),
    ADD COLUMN grant_action TEXT NOT NULL DEFAULT 'NONE'
        CHECK (grant_action IN ('NONE', 'REPLACE_QUEUED_GRANTS', 'START_RETRY_ATTEMPT')),
    ADD COLUMN grant_refresh_deadline_at TIMESTAMPTZ NULL,
    ADD COLUMN grant_renewal_cutoff_at TIMESTAMPTZ NULL,
    ADD COLUMN current_grants_encrypted BYTEA NULL;

UPDATE trading_review_jobs
SET grant_renewal_cutoff_at = created_at + INTERVAL '1 hour';

ALTER TABLE trading_review_jobs
    ALTER COLUMN grant_renewal_cutoff_at SET NOT NULL,
    ALTER COLUMN grant_renewal_cutoff_at SET DEFAULT (CURRENT_TIMESTAMP + INTERVAL '1 hour');

ALTER TABLE trading_review_jobs
    ADD CONSTRAINT trading_review_grant_wait_shape_check CHECK (
        (grant_action = 'NONE' AND grant_refresh_deadline_at IS NULL)
        OR (grant_action <> 'NONE' AND grant_refresh_deadline_at IS NOT NULL
            AND status IN ('PENDING', 'RUNNING'))
    );

CREATE TABLE trading_review_grant_revisions (
    job_id UUID NOT NULL REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    grant_revision BIGINT NOT NULL CHECK (grant_revision >= 0),
    artifact_role TEXT NOT NULL CHECK (artifact_role IN ('TRADE_HISTORY', 'POSITION_HISTORY')),
    core_artifact_id TEXT NOT NULL,
    raw_sha256 CHAR(64) NOT NULL,
    grant_expires_at TIMESTAMPTZ NOT NULL,
    attempt_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'REPLACED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMPTZ NULL,
    PRIMARY KEY (job_id, grant_revision, artifact_role),
    CONSTRAINT trading_review_grant_revision_state_check CHECK (
        (status = 'ACTIVE' AND closed_at IS NULL)
        OR (status <> 'ACTIVE' AND closed_at IS NOT NULL)
    )
);

CREATE TABLE trading_review_grant_update_receipts (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    request_fingerprint CHAR(64) NOT NULL,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX trading_review_grant_receipt_revision_idx
    ON trading_review_grant_update_receipts (job_id, (receipt->>'grantRevision'));

CREATE TABLE trading_review_attempt_tokens (
    job_id UUID NOT NULL REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    attempt_token TEXT NOT NULL,
    PRIMARY KEY (job_id, attempt_token)
);

INSERT INTO trading_review_attempt_tokens (job_id, attempt_token)
SELECT job_id, attempt_token FROM trading_review_jobs;

INSERT INTO trading_review_grant_revisions
    (job_id, grant_revision, artifact_role, core_artifact_id, raw_sha256,
     grant_expires_at, attempt_token, status, closed_at)
SELECT j.job_id, 0, artifact->>'role', artifact->>'coreArtifactId',
       artifact->>'rawSha256', (artifact->>'expiresAt')::timestamptz,
       j.attempt_token,
       CASE WHEN j.status = 'PENDING' THEN 'ACTIVE' ELSE 'CONSUMED' END,
       CASE WHEN j.status = 'PENDING' THEN NULL ELSE CURRENT_TIMESTAMP END
FROM trading_review_jobs AS j
CROSS JOIN LATERAL jsonb_array_elements(j.request_payload->'artifacts') AS artifact;

ALTER TABLE trading_review_terminal_payloads
    ALTER COLUMN payload_bytes DROP NOT NULL;

ALTER TABLE trading_review_terminal_payloads
    ADD COLUMN acknowledgement_disposition TEXT NULL
        CHECK (acknowledgement_disposition IN ('PERSISTED', 'DISCARDED_STALE')),
    ADD COLUMN current_deletion_generation BIGINT NULL,
    ADD COLUMN purged_at TIMESTAMPTZ NULL,
    ADD CONSTRAINT trading_review_payload_purge_shape_check CHECK (
        (purged_at IS NULL AND payload_bytes IS NOT NULL)
        OR (purged_at IS NOT NULL AND payload_bytes IS NULL)
    );

CREATE INDEX trading_review_grant_revisions_active_idx
    ON trading_review_grant_revisions (grant_expires_at, job_id)
    WHERE status = 'ACTIVE';

CREATE INDEX trading_review_jobs_grant_wait_idx
    ON trading_review_jobs (grant_refresh_deadline_at, job_id)
    WHERE grant_action <> 'NONE';
