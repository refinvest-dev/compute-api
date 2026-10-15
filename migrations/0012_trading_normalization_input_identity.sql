-- Legacy rows retain their old hashes; they are never relabelled as ADR-065 inputSetHash.
ALTER TABLE trading_review_jobs
    ADD COLUMN core_normalization_run_id TEXT NULL,
    ADD COLUMN input_identity JSONB NULL,
    ADD COLUMN expected_input_set_hash CHAR(64) NULL,
    ADD COLUMN computed_input_set_hash CHAR(64) NULL,
    ADD COLUMN hash_specification_version TEXT NULL,
    ADD COLUMN retry_policy_version TEXT NULL,
    ADD COLUMN target_version_set JSONB NULL;

ALTER TABLE trading_review_jobs ALTER COLUMN expected_input_hash DROP NOT NULL;

ALTER TABLE trading_review_jobs ADD CONSTRAINT trading_review_identity_v1_shape CHECK (
    (core_normalization_run_id IS NULL AND input_identity IS NULL
        AND expected_input_set_hash IS NULL AND hash_specification_version IS NULL)
    OR (core_normalization_run_id IS NOT NULL AND input_identity IS NOT NULL
        AND expected_input_set_hash IS NOT NULL AND hash_specification_version IS NOT NULL
        AND retry_policy_version IS NOT NULL AND target_version_set IS NOT NULL)
);

-- Old active jobs cannot be run against the new contract without inventing identity.
UPDATE trading_review_jobs
SET status = 'FAILED', progress_stage = 'LEGACY_IDENTITY_UNAVAILABLE',
    failure = '{"code":"VERSION_INCOMPATIBLE","message":"Normalization identity is unavailable for this job.","retryable":false,"stage":"VALIDATION"}'::jsonb,
    terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
    current_grants_encrypted = NULL
WHERE status IN ('PENDING', 'RUNNING') AND core_normalization_run_id IS NULL;

ALTER TABLE trading_review_terminal_payloads
    ADD COLUMN input_set_hash CHAR(64) NULL,
    ADD COLUMN reconciliation_manifest_hash CHAR(64) NULL;
ALTER TABLE trading_review_terminal_payloads ALTER COLUMN input_hash DROP NOT NULL;
