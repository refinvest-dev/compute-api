ALTER TABLE compute_backtest_jobs
    ADD COLUMN idempotency_key_hash TEXT NULL,
    ADD COLUMN request_fingerprint TEXT NULL;

ALTER TABLE compute_backtest_jobs
    ADD CONSTRAINT compute_backtest_jobs_idempotency_pair_check CHECK (
        (idempotency_key_hash IS NULL AND request_fingerprint IS NULL)
        OR (idempotency_key_hash IS NOT NULL AND request_fingerprint IS NOT NULL)
    );

CREATE UNIQUE INDEX compute_backtest_jobs_idempotency_key_hash_idx
    ON compute_backtest_jobs (idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;
