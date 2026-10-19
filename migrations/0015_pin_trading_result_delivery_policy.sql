-- Legacy jobs have no Core-advertised limit. They cannot be claimed under V1.
-- Existing terminal rows retain their TTL; a policy-less descriptor is unavailable.
ALTER TABLE trading_review_jobs
    ADD COLUMN result_delivery_policy JSONB NULL;

ALTER TABLE trading_review_jobs
    ADD CONSTRAINT trading_review_result_delivery_policy_shape CHECK (
        result_delivery_policy IS NULL OR (
            jsonb_typeof(result_delivery_policy) = 'object'
            AND result_delivery_policy->>'policyVersion' = 'RESULT_DELIVERY_V1'
            AND result_delivery_policy->>'mode' = 'SINGLE_RESOURCE'
            AND jsonb_typeof(result_delivery_policy->'maxAcceptedPayloadBytes') = 'number'
            AND (result_delivery_policy->>'maxAcceptedPayloadBytes')::BIGINT > 0
            AND result_delivery_policy->'acceptedContentEncodings' = '["identity"]'::jsonb
        )
    );

-- A policy cannot be reconstructed from older requests. End active legacy jobs
-- safely so that normal terminal TTL cleanup can remove them.
UPDATE trading_review_jobs
SET status = 'FAILED', progress_stage = 'LEGACY_RESULT_DELIVERY_POLICY_UNAVAILABLE',
    failure = '{"code":"VERSION_INCOMPATIBLE","message":"Result delivery policy is unavailable for this job.","retryable":false,"stage":"VALIDATION"}'::jsonb,
    terminal_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
    claimed_worker = NULL, lease_owner = NULL, lease_expires_at = NULL,
    heartbeat_at = NULL, next_attempt_at = NULL,
    current_grants_encrypted = NULL, grant_action = 'NONE',
    grant_refresh_deadline_at = NULL
WHERE status IN ('PENDING', 'RUNNING') AND result_delivery_policy IS NULL;

CREATE FUNCTION trading_review_result_delivery_policy_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.result_delivery_policy IS NOT NULL
       AND NEW.result_delivery_policy IS DISTINCT FROM OLD.result_delivery_policy THEN
        RAISE EXCEPTION 'Result delivery policy is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trading_review_result_delivery_policy_immutable_trigger
BEFORE UPDATE ON trading_review_jobs
FOR EACH ROW EXECUTE FUNCTION trading_review_result_delivery_policy_immutable();
