CREATE TABLE compute_backtest_jobs (
    run_id UUID PRIMARY KEY,
    strategy_version_id TEXT NULL,
    dataset_snapshot_id TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
    request_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
