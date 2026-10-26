-- All existing normalization jobs were admitted under the V1-only producer.
-- Their request snapshots receive an explicit V1 pin; new admissions provide both fields.
ALTER TABLE trading_review_jobs
    ADD COLUMN result_schema_version TEXT,
    ADD COLUMN result_hash_specification_version TEXT;

UPDATE trading_review_jobs
SET result_schema_version = 'TRADING_NORMALIZATION_RESULT_V1',
    result_hash_specification_version = 'TRADING_NORMALIZATION_RESULT_HASH_V1',
    request_payload = request_payload || jsonb_build_object(
        'resultSchemaVersion', 'TRADING_NORMALIZATION_RESULT_V1',
        'resultHashSpecificationVersion', 'TRADING_NORMALIZATION_RESULT_HASH_V1'
    );

ALTER TABLE trading_review_jobs
    ALTER COLUMN result_schema_version SET NOT NULL,
    ALTER COLUMN result_hash_specification_version SET NOT NULL,
    ADD CONSTRAINT trading_review_job_result_pair_check CHECK (
        (result_schema_version = 'TRADING_NORMALIZATION_RESULT_V1'
            AND result_hash_specification_version = 'TRADING_NORMALIZATION_RESULT_HASH_V1')
        OR (result_schema_version = 'TRADING_NORMALIZATION_RESULT_V2'
            AND result_hash_specification_version = 'TRADING_NORMALIZATION_RESULT_HASH_V2')
    ),
    ADD CONSTRAINT trading_review_job_result_request_pin_check CHECK (
        (request_payload->>'resultSchemaVersion') IS NOT DISTINCT FROM result_schema_version
        AND (request_payload->>'resultHashSpecificationVersion')
            IS NOT DISTINCT FROM result_hash_specification_version
    );

CREATE FUNCTION trading_review_result_pair_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.result_schema_version, NEW.result_hash_specification_version)
       IS DISTINCT FROM ROW(OLD.result_schema_version, OLD.result_hash_specification_version)
       OR NEW.request_payload->>'resultSchemaVersion'
          IS DISTINCT FROM OLD.request_payload->>'resultSchemaVersion'
       OR NEW.request_payload->>'resultHashSpecificationVersion'
          IS DISTINCT FROM OLD.request_payload->>'resultHashSpecificationVersion' THEN
        RAISE EXCEPTION 'Normalization result version pin is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trading_review_result_pair_immutable_trigger
BEFORE UPDATE ON trading_review_jobs
FOR EACH ROW EXECUTE FUNCTION trading_review_result_pair_immutable();

ALTER TABLE trading_review_terminal_payloads
    DROP CONSTRAINT trading_review_terminal_integrity_shape_check,
    ADD CONSTRAINT trading_review_terminal_integrity_shape_check CHECK (
        (result_schema_version IS NULL AND result_hash_specification_version IS NULL
            AND result_counts IS NULL AND section_digests IS NULL
            AND result_digest_manifest IS NULL)
        OR ((result_schema_version = 'TRADING_NORMALIZATION_RESULT_V1'
                AND result_hash_specification_version = 'TRADING_NORMALIZATION_RESULT_HASH_V1')
            OR (result_schema_version = 'TRADING_NORMALIZATION_RESULT_V2'
                AND result_hash_specification_version = 'TRADING_NORMALIZATION_RESULT_HASH_V2'))
            AND result_counts IS NOT NULL AND section_digests IS NOT NULL
            AND result_digest_manifest IS NOT NULL
    );
