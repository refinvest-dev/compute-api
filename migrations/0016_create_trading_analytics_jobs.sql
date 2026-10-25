-- ADR-069: Analytics has distinct semantic identity, attempts, grants and terminal payloads.
CREATE TABLE trading_analytics_jobs (
    job_id UUID PRIMARY KEY,
    core_logical_job_id TEXT NOT NULL,
    core_analysis_run_id TEXT NOT NULL UNIQUE,
    ledger_revision_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    progress_stage TEXT NOT NULL DEFAULT 'QUEUED',
    request_payload JSONB NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    expected_input_hash CHAR(64) NOT NULL,
    analysis_config_hash CHAR(64) NOT NULL,
    version_set_hash CHAR(64) NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number BETWEEN 1 AND 3),
    attempt_token TEXT NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation >= 0),
    grant_revision BIGINT NOT NULL DEFAULT 0 CHECK (grant_revision >= 0),
    grants_encrypted BYTEA,
    grants_expire_at TIMESTAMPTZ,
    lease_generation BIGINT NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    last_attempt_failure JSONB,
    retry_available BOOLEAN NOT NULL DEFAULT FALSE,
    retry_cutoff_at TIMESTAMPTZ NOT NULL,
    failure JSONB,
    terminal_payload_id UUID,
    result_hash CHAR(64),
    cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    terminal_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT trading_analytics_terminal_state_check CHECK (
        (status IN ('PENDING', 'RUNNING') AND terminal_at IS NULL)
        OR (status IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminal_at IS NOT NULL)
    ),
    CONSTRAINT trading_analytics_result_state_check CHECK (
        status <> 'COMPLETED' OR
        (terminal_payload_id IS NOT NULL AND result_hash IS NOT NULL AND failure IS NULL)
    ),
    CONSTRAINT trading_analytics_failure_state_check CHECK (
        status <> 'FAILED' OR failure IS NOT NULL
    )
);

CREATE TABLE trading_analytics_attempts (
    job_id UUID NOT NULL REFERENCES trading_analytics_jobs(job_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    attempt_token TEXT NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation >= 0),
    lease_generation BIGINT NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    failure JSONB,
    outcome TEXT CHECK (outcome IN ('COMPLETED', 'FAILED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, attempt_number),
    UNIQUE (job_id, attempt_token)
);

CREATE TABLE trading_analytics_idempotency (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    request_fingerprint CHAR(64) NOT NULL,
    job_id UUID NOT NULL UNIQUE REFERENCES trading_analytics_jobs(job_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE trading_analytics_grant_updates (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES trading_analytics_jobs(job_id) ON DELETE CASCADE,
    request_fingerprint CHAR(64) NOT NULL,
    receipt JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE trading_analytics_cancellations (
    idempotency_key_hash CHAR(64) PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES trading_analytics_jobs(job_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE trading_analytics_payloads (
    payload_id UUID PRIMARY KEY,
    job_id UUID NOT NULL UNIQUE REFERENCES trading_analytics_jobs(job_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    attempt_token TEXT NOT NULL,
    deletion_generation BIGINT NOT NULL,
    encrypted_bytes BYTEA,
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    payload_sha256 CHAR(64) NOT NULL,
    descriptor JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ,
    CONSTRAINT trading_analytics_payload_attempt_fk
        FOREIGN KEY (job_id, attempt_number)
        REFERENCES trading_analytics_attempts(job_id, attempt_number),
    CONSTRAINT trading_analytics_payload_expiry CHECK (expires_at > created_at)
);

ALTER TABLE trading_analytics_jobs
    ADD CONSTRAINT trading_analytics_terminal_payload_fk
    FOREIGN KEY (terminal_payload_id) REFERENCES trading_analytics_payloads(payload_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE trading_analytics_ack_receipts (
    job_id UUID PRIMARY KEY REFERENCES trading_analytics_jobs(job_id) ON DELETE CASCADE,
    request_fingerprint CHAR(64) NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('PERSISTED', 'DISCARDED_STALE')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX trading_analytics_claimable_idx
    ON trading_analytics_jobs(status, lease_expires_at, created_at)
    WHERE status IN ('PENDING', 'RUNNING');
CREATE INDEX trading_analytics_cleanup_idx
    ON trading_analytics_jobs(terminal_at, job_id)
    WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED');
CREATE INDEX trading_analytics_payload_expiry_idx
    ON trading_analytics_payloads(expires_at, payload_id);
CREATE UNIQUE INDEX trading_analytics_active_logical_idx
    ON trading_analytics_jobs(core_logical_job_id)
    WHERE status IN ('PENDING', 'RUNNING');
