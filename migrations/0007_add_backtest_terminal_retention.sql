ALTER TABLE compute_backtest_jobs
    ADD COLUMN terminal_at TIMESTAMPTZ NULL;

UPDATE compute_backtest_jobs
SET terminal_at = COALESCE(completed_at, created_at)
WHERE status IN ('COMPLETED', 'FAILED');

ALTER TABLE compute_backtest_jobs
    ADD CONSTRAINT compute_backtest_jobs_terminal_timestamp_check CHECK (
        (status IN ('COMPLETED', 'FAILED') AND terminal_at IS NOT NULL)
        OR (status IN ('PENDING', 'RUNNING') AND terminal_at IS NULL)
    );

CREATE INDEX compute_backtest_jobs_terminal_cleanup_idx
    ON compute_backtest_jobs (terminal_at)
    WHERE status IN ('COMPLETED', 'FAILED');
