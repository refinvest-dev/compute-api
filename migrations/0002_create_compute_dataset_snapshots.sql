CREATE TABLE compute_dataset_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    coverage JSONB NOT NULL,
    adjustment_policy TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    UNIQUE (storage_path)
);

ALTER TABLE compute_backtest_jobs
    ADD COLUMN resolved_dataset_snapshot_id TEXT NULL;
