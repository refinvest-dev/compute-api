-- V1 terminal integrity is persisted atomically with the encrypted resource.
-- Historical internal hashes remain legacy rows; they are never reinterpreted as V1.
ALTER TABLE trading_review_terminal_payloads
    ADD COLUMN result_schema_version TEXT NULL,
    ADD COLUMN result_hash_specification_version TEXT NULL,
    ADD COLUMN result_counts JSONB NULL,
    ADD COLUMN section_digests JSONB NULL,
    ADD COLUMN result_digest_manifest JSONB NULL;

ALTER TABLE trading_review_terminal_payloads
    ADD CONSTRAINT trading_review_terminal_integrity_shape_check CHECK (
        (result_schema_version IS NULL AND result_hash_specification_version IS NULL
            AND result_counts IS NULL AND section_digests IS NULL AND result_digest_manifest IS NULL)
        OR
        (result_schema_version = 'TRADING_NORMALIZATION_RESULT_V1'
            AND result_hash_specification_version = 'TRADING_NORMALIZATION_RESULT_HASH_V1'
            AND result_counts IS NOT NULL AND section_digests IS NOT NULL
            AND result_digest_manifest IS NOT NULL)
    );
