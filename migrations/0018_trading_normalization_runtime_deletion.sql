CREATE TABLE trading_normalization_deletions (
    core_normalization_run_id TEXT PRIMARY KEY,
    core_import_session_id TEXT NOT NULL,
    source_deletion_generation BIGINT NOT NULL CHECK (source_deletion_generation >= 0),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_absent_at TIMESTAMPTZ
);

CREATE TABLE trading_normalization_deletion_receipts (
    core_normalization_run_id TEXT NOT NULL REFERENCES trading_normalization_deletions(core_normalization_run_id) ON DELETE CASCADE,
    deletion_request_id TEXT NOT NULL,
    deletion_generation BIGINT NOT NULL CHECK (deletion_generation > 0),
    PRIMARY KEY (core_normalization_run_id, deletion_request_id)
);

-- Independent from the queue lease: lease expiry is not proof that a child has exited.
CREATE TABLE trading_normalization_executions (
    job_id UUID NOT NULL REFERENCES trading_review_jobs(job_id) ON DELETE CASCADE,
    lease_owner TEXT NOT NULL,
    host_name TEXT,
    parent_pid BIGINT,
    parent_identity TEXT,
    child_pid BIGINT,
    child_identity TEXT,
    released_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, lease_owner)
);
CREATE INDEX trading_normalization_executions_live_idx ON trading_normalization_executions(job_id)
    WHERE released_at IS NULL;

-- Preserve unknown legacy live resources; never manufacture an absence receipt.
INSERT INTO trading_normalization_executions(job_id,lease_owner)
SELECT job_id,COALESCE(lease_owner,'legacy-unverified') FROM trading_review_jobs WHERE status='RUNNING';
