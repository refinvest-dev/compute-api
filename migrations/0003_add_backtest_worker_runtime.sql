ALTER TABLE compute_backtest_jobs
    ADD COLUMN worker_id TEXT NULL,
    ADD COLUMN lease_expires_at TIMESTAMPTZ NULL,
    ADD COLUMN heartbeat_at TIMESTAMPTZ NULL,
    ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN next_attempt_at TIMESTAMPTZ NULL,
    ADD COLUMN failure_reason TEXT NULL,
    ADD COLUMN error_code TEXT NULL;

CREATE INDEX compute_backtest_jobs_claimable_idx
    ON compute_backtest_jobs (status, next_attempt_at, lease_expires_at);
