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

-- Recompute the Python semantic request fingerprint after inserting the explicit pin.
-- JSON object keys use UTF-8/C order, and grant/term arrays follow the producer's
-- normalization_request_fingerprint ordering and ephemeral-grant exclusions.
CREATE FUNCTION trading_review_fingerprint_json(node JSONB)
RETURNS TEXT LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    encoded TEXT;
BEGIN
    CASE jsonb_typeof(node)
        WHEN 'object' THEN
            SELECT '{' || COALESCE(string_agg(
                to_jsonb(key)::TEXT || ':' || trading_review_fingerprint_json(value),
                ',' ORDER BY key COLLATE "C"
            ), '') || '}'
            INTO encoded FROM jsonb_each(node);
            RETURN encoded;
        WHEN 'array' THEN
            SELECT '[' || COALESCE(string_agg(
                trading_review_fingerprint_json(value),
                ',' ORDER BY ordinal
            ), '') || ']'
            INTO encoded FROM jsonb_array_elements(node) WITH ORDINALITY AS item(value, ordinal);
            RETURN encoded;
        ELSE
            RETURN node::TEXT;
    END CASE;
END;
$$;

WITH normalized AS (
    SELECT j.job_id,
           jsonb_set(
               jsonb_set(j.request_payload, '{artifactAccessGrants}',
                   COALESCE((SELECT jsonb_agg(grant_node - 'accessUri' - 'expiresAt'
                                              ORDER BY grant_node->>'role' COLLATE "C")
                             FROM jsonb_array_elements(
                                 j.request_payload->'artifactAccessGrants'
                             ) AS grant_item(grant_node)), '[]'::jsonb)),
               '{pinnedInstrumentTerms}',
               COALESCE((SELECT jsonb_agg(term_node
                                          ORDER BY term_node->>'instrumentId' COLLATE "C")
                         FROM jsonb_array_elements(
                             j.request_payload->'pinnedInstrumentTerms'
                         ) AS term_item(term_node)), '[]'::jsonb)
           ) AS request_for_fingerprint
    FROM trading_review_jobs AS j
), fingerprints AS (
    SELECT job_id,
           encode(sha256(convert_to(
               trading_review_fingerprint_json(request_for_fingerprint), 'UTF8'
           )), 'hex') AS request_fingerprint
    FROM normalized
)
UPDATE trading_review_jobs AS j
SET request_fingerprint = f.request_fingerprint
FROM fingerprints AS f
WHERE j.job_id = f.job_id;

UPDATE trading_review_job_idempotency AS i
SET request_fingerprint = j.request_fingerprint
FROM trading_review_jobs AS j
WHERE i.job_id = j.job_id;

DROP FUNCTION trading_review_fingerprint_json(JSONB);

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
