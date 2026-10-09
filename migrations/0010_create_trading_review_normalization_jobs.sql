CREATE TABLE trading_review_jobs (
    job_id UUID PRIMARY KEY,
    core_logical_job_id TEXT NOT NULL,
    core_import_session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'NORMALIZATION_RECONCILIATION'),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    progress_stage TEXT NOT NULL DEFAULT 'QUEUED',
    request_payload JSONB NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    expected_input_hash CHAR(64) NOT NULL,
    version_set_hash CHAR(64) NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation >= 0),
    attempt_token TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_worker TEXT NULL,
    lease_owner TEXT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    heartbeat_at TIMESTAMPTZ NULL,
    next_attempt_at TIMESTAMPTZ NULL,
    cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    failure JSONB NULL,
    result_hash CHAR(64) NULL,
    terminal_payload_id UUID NULL,
    started_at TIMESTAMPTZ NULL,
    terminal_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT trading_review_jobs_terminal_shape_check CHECK (
        (status IN ('PENDING', 'RUNNING') AND terminal_at IS NULL)
        OR (status IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminal_at IS NOT NULL)
    ),
    CONSTRAINT trading_review_jobs_completed_shape_check CHECK (
        status <> 'COMPLETED'
        OR (terminal_payload_id IS NOT NULL AND result_hash IS NOT NULL AND failure IS NULL)
    ),
    CONSTRAINT trading_review_jobs_failed_shape_check CHECK (
        status <> 'FAILED' OR failure IS NOT NULL
    )
);

CREATE TABLE trading_review_job_idempotency (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    request_fingerprint CHAR(64) NOT NULL,
    job_id UUID NOT NULL UNIQUE REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE trading_review_job_attempts (
    job_id UUID NOT NULL REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    attempt_token TEXT NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation >= 0),
    lease_owner TEXT NOT NULL,
    lease_started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NULL,
    outcome TEXT NULL CHECK (outcome IS NULL OR outcome IN ('COMPLETED', 'FAILED', 'CANCELLED', 'RETRY', 'EXPIRED')),
    PRIMARY KEY (job_id, attempt_number)
);

CREATE TABLE trading_review_artifact_leases (
    lease_id UUID PRIMARY KEY,
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    artifact_role TEXT NOT NULL CHECK (artifact_role IN ('TRADE_HISTORY', 'POSITION_HISTORY')),
    core_artifact_id TEXT NOT NULL,
    raw_sha256 CHAR(64) NOT NULL,
    grant_expires_at TIMESTAMPTZ NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation >= 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMPTZ NULL,
    CONSTRAINT trading_review_artifact_lease_attempt_fk
        FOREIGN KEY (job_id, attempt_number)
        REFERENCES trading_review_job_attempts(job_id, attempt_number) ON DELETE CASCADE,
    CONSTRAINT trading_review_artifact_lease_unique_role
        UNIQUE (job_id, attempt_number, artifact_role),
    CONSTRAINT trading_review_artifact_lease_state_check CHECK (
        (status = 'ACTIVE' AND closed_at IS NULL)
        OR (status <> 'ACTIVE' AND closed_at IS NOT NULL)
    )
);

CREATE TABLE trading_review_terminal_payloads (
    payload_id UUID PRIMARY KEY,
    job_id UUID NOT NULL UNIQUE REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    attempt_token TEXT NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation >= 0),
    media_type TEXT NOT NULL CHECK (
        media_type = 'application/vnd.refinvest.trading-normalization+json'
    ),
    payload_bytes BYTEA NOT NULL,
    encryption_version TEXT NOT NULL DEFAULT 'AES256_GCM_V1'
        CHECK (encryption_version = 'AES256_GCM_V1'),
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    payload_sha256 CHAR(64) NOT NULL,
    input_hash CHAR(64) NOT NULL,
    version_set_hash CHAR(64) NOT NULL,
    result_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ NULL,
    acknowledged_core_aggregate_id TEXT NULL,
    persisted_at TIMESTAMPTZ NULL,
    CONSTRAINT trading_review_payload_attempt_fk
        FOREIGN KEY (job_id, attempt_number)
        REFERENCES trading_review_job_attempts(job_id, attempt_number),
    CONSTRAINT trading_review_payload_expiry_check CHECK (expires_at > created_at),
    CONSTRAINT trading_review_payload_ack_shape_check CHECK (
        (acknowledged_at IS NULL AND acknowledged_core_aggregate_id IS NULL AND persisted_at IS NULL)
        OR (acknowledged_at IS NOT NULL AND acknowledged_core_aggregate_id IS NOT NULL AND persisted_at IS NOT NULL)
    )
);

ALTER TABLE trading_review_jobs
    ADD CONSTRAINT trading_review_jobs_terminal_payload_fk
    FOREIGN KEY (terminal_payload_id)
    REFERENCES trading_review_terminal_payloads(payload_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE trading_review_job_cancellations (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE trading_review_job_acknowledgements (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    job_id UUID NOT NULL,
    payload_id UUID NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '24 hours'
);

CREATE INDEX trading_review_jobs_claimable_idx
    ON trading_review_jobs (status, next_attempt_at, lease_expires_at, created_at)
    WHERE status IN ('PENDING', 'RUNNING');

CREATE UNIQUE INDEX trading_review_jobs_one_active_logical_idx
    ON trading_review_jobs (core_logical_job_id)
    WHERE status IN ('PENDING', 'RUNNING');

CREATE INDEX trading_review_jobs_terminal_cleanup_idx
    ON trading_review_jobs (terminal_at, job_id)
    WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED');

CREATE INDEX trading_review_terminal_payloads_cleanup_idx
    ON trading_review_terminal_payloads (acknowledged_at, expires_at, payload_id);

CREATE INDEX trading_review_job_attempts_live_lease_idx
    ON trading_review_job_attempts (lease_expires_at)
    WHERE completed_at IS NULL;

CREATE INDEX trading_review_artifact_leases_expiry_idx
    ON trading_review_artifact_leases (grant_expires_at)
    WHERE status = 'ACTIVE';

CREATE INDEX trading_review_job_acknowledgements_cleanup_idx
    ON trading_review_job_acknowledgements (expires_at);
