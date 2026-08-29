ALTER TABLE compute_backtest_jobs
    ADD COLUMN result_payload JSONB NULL,
    ADD COLUMN actual_period_start DATE NULL,
    ADD COLUMN actual_period_end DATE NULL,
    ADD COLUMN engine_version TEXT NULL,
    ADD COLUMN completed_at TIMESTAMPTZ NULL;

ALTER TABLE compute_backtest_jobs
    ADD CONSTRAINT compute_backtest_jobs_completed_result_check CHECK (
        status <> 'COMPLETED' OR (
            result_payload IS NOT NULL
            AND actual_period_start IS NOT NULL
            AND actual_period_end IS NOT NULL
            AND engine_version IS NOT NULL
            AND completed_at IS NOT NULL
        )
    );
