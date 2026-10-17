-- Once stored, only retention/ACK fields may change on a V1 terminal resource.
CREATE FUNCTION trading_review_terminal_integrity_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(
        NEW.result_schema_version, NEW.result_hash_specification_version,
        NEW.result_counts, NEW.section_digests, NEW.result_digest_manifest,
        NEW.reconciliation_manifest_hash, NEW.result_hash, NEW.input_set_hash,
        NEW.version_set_hash, NEW.payload_sha256, NEW.byte_size
    ) IS DISTINCT FROM ROW(
        OLD.result_schema_version, OLD.result_hash_specification_version,
        OLD.result_counts, OLD.section_digests, OLD.result_digest_manifest,
        OLD.reconciliation_manifest_hash, OLD.result_hash, OLD.input_set_hash,
        OLD.version_set_hash, OLD.payload_sha256, OLD.byte_size
    ) THEN
        RAISE EXCEPTION 'Terminal result integrity metadata is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trading_review_terminal_integrity_immutable_trigger
BEFORE UPDATE ON trading_review_terminal_payloads
FOR EACH ROW EXECUTE FUNCTION trading_review_terminal_integrity_immutable();
