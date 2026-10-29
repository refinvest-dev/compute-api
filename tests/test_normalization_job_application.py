"""Admission ordering and job commands through an HTTP/SQL-independent port."""

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from refinvest.application.normalization_jobs import (
    NormalizationArtifactGrant,
    NormalizationInstrumentTerms,
    NormalizationJobIdentityUnavailable,
    NormalizationJobNotFound,
    NormalizationRequestInvalid,
    NormalizationResultUnavailable,
    SubmitNormalizationJob,
    UpdateNormalizationGrants,
    cancel_normalization_job,
    get_normalization_job_status,
    get_normalization_result,
    submit_normalization_job,
    update_normalization_grants,
)
from refinvest.application.trading_review_jobs import (
    TerminalPayloadDescriptor,
    TradingReviewCapacityExceeded,
    TradingReviewGrantInvalid,
    TradingReviewIdempotencyConflict,
    TradingReviewJobStateConflict,
    TradingReviewJobStatus,
    hash_idempotency_key,
    normalization_request_fingerprint,
)
from refinvest.contracts.normalization.identity import (
    NormalizationInputIdentity,
    calculate_input_set_hash,
)
from tests.test_trading_review_routes import MemoryJobs, _body


def _command() -> SubmitNormalizationJob:
    payload = _body()
    return SubmitNormalizationJob(
        payload=payload,
        identity=NormalizationInputIdentity.model_validate(payload["inputIdentity"]),
        artifacts=tuple(
            NormalizationArtifactGrant(item["coreArtifactId"], item["role"], False)
            for item in payload["artifactAccessGrants"]
        ),
        terms=tuple(
            NormalizationInstrumentTerms(
                item["instrumentId"],
                item["instrumentTermsVersion"],
                item["settlementAsset"],
                item["quantityStep"],
                item["priceTick"],
                item["contractMultiplier"],
            )
            for item in payload["pinnedInstrumentTerms"]
        ),
    )


class RecordingJobs(MemoryJobs):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.error = None

    def create_idempotently(self, job, key):
        self.calls.append(("create", job, key))
        if self.error is not None:
            raise self.error
        return super().create_idempotently(job, key)

    def get(self, job_id):
        self.calls.append(("get", job_id))
        if self.error is not None:
            raise self.error
        return super().get(job_id)

    def get_result_descriptor(self, job_id):
        self.calls.append(("descriptor", job_id))
        return super().get_result_descriptor(job_id)

    def request_cancellation(self, job_id, key):
        self.calls.append(("cancel", job_id, key))
        return super().request_cancellation(job_id, key)

    def update_grants(self, job_id, key, payload):
        self.calls.append(("grants", job_id, key, payload))
        return super().update_grants(job_id, key, payload)


@pytest.mark.parametrize("version", ["V1", "V2"])
def test_admission_preserves_pins_hashes_and_the_single_durable_projection(version):
    command = _command()
    command.payload["resultSchemaVersion"] = "TRADING_NORMALIZATION_RESULT_" + version
    command.payload["resultHashSpecificationVersion"] = (
        "TRADING_NORMALIZATION_RESULT_HASH_" + version
    )
    before = copy.deepcopy(command.payload)
    repo = RecordingJobs()
    accepted = submit_normalization_job(command, "raw-key", repo)
    operation, job, key = repo.calls[0]
    assert operation == "create" and len(repo.calls) == 1
    assert key == hash_idempotency_key("raw-key")
    assert job.request_payload is command.payload
    assert command.payload == before
    assert job.request_fingerprint == normalization_request_fingerprint(before)
    assert job.computed_input_set_hash == calculate_input_set_hash(command.identity)
    assert accepted.result_schema_version == command.payload["resultSchemaVersion"]


@pytest.mark.parametrize(
    ("first", "code", "message"),
    [
        ("version", "VERSION_INCOMPATIBLE", "Normalizer VersionSet is unsupported."),
        ("roles", "FILE_FORMAT_INVALID", "Exactly one artifact per role is required."),
        ("artifact-ids", "FILE_FORMAT_INVALID", "Artifact identities must be distinct."),
        ("uri", "ARTIFACT_ACCESS_DENIED", "Artifact grant URI is invalid."),
        ("timezone", "FILE_FORMAT_INVALID", "Timezone is unsupported."),
        ("terms", "INSTRUMENT_TERMS_UNAVAILABLE", "Pinned instrument terms are required."),
        ("settlement", "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument settlement asset is invalid."),
        ("grid", "INSTRUMENT_TERMS_UNAVAILABLE", "Instrument grid is invalid."),
    ],
)
def test_multiple_invalid_facts_preserve_first_failure_and_never_call_store(first, code, message):
    command = _command()
    # Every case also has a bad grid; earlier failures must keep their original priority.
    command = replace(command, terms=(replace(command.terms[0], quantity_step="NaN"),))
    if first == "version":
        target = command.identity.target_version_set
        target = target.model_copy(
            update={
                "normalizer": target.normalizer.model_copy(update={"normalizer_version": "latest"})
            }
        )
        command = replace(
            command, identity=command.identity.model_copy(update={"target_version_set": target})
        )
    elif first in {"roles", "artifact-ids", "uri"}:
        artifact = replace(command.artifacts[0], has_credentials_or_fragment=True)
        if first == "roles":
            artifact = replace(artifact, role="OTHER")
        if first == "artifact-ids":
            artifact = replace(artifact, core_artifact_id=command.artifacts[1].core_artifact_id)
        command = replace(command, artifacts=(artifact, command.artifacts[1]))
    elif first == "timezone":
        # Transport identity validation normally rejects this too; no HTTP model is needed here.
        command = replace(
            command,
            identity=command.identity.model_copy(update={"source_timezone": "missing/zone"}),
        )
    elif first == "terms":
        # Unknown hash specs retain deferred worker rejection and skip the registry check.
        command = replace(
            command,
            identity=command.identity.model_copy(update={"hash_specification_version": "FUTURE"}),
            terms=(),
        )
    elif first == "settlement":
        command = replace(command, terms=(replace(command.terms[0], settlement_asset="OTHER"),))
    repo = RecordingJobs()
    with pytest.raises(NormalizationRequestInvalid) as caught:
        submit_normalization_job(command, "key", repo)
    assert caught.value.code == code and str(caught.value) == message
    assert repo.calls == []


def test_unknown_hash_and_expired_replay_grants_are_not_rejected_ahead_of_store():
    command = _command()
    command.payload["inputIdentity"]["hashSpecificationVersion"] = "FUTURE"
    command = replace(
        command,
        identity=NormalizationInputIdentity.model_validate(command.payload["inputIdentity"]),
    )
    for grant in command.payload["artifactAccessGrants"]:
        grant["expiresAt"] = "2000-01-01T00:00:00Z"
    repo = RecordingJobs()
    submit_normalization_job(command, "key", repo)
    assert repo.calls[0][1].computed_input_set_hash is None
    # Freshness/replay/capacity ordering belongs to the existing atomic repository operation.
    assert len(repo.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        TradingReviewIdempotencyConflict(),
        TradingReviewCapacityExceeded(),
        TradingReviewGrantInvalid(),
    ],
)
def test_admission_preserves_application_errors_from_atomic_store(error):
    repo = RecordingJobs()
    repo.error = error
    with pytest.raises(type(error)) as caught:
        submit_normalization_job(_command(), "key", repo)
    assert caught.value is error


def test_grant_and_cancellation_hash_command_keys_and_forward_exact_payload():
    command = _command()
    repo = RecordingJobs()
    accepted = submit_normalization_job(command, "create", repo)
    payload = {"mode": "REPLACE_QUEUED_GRANTS", "nextAttemptToken": "next"}
    receipt = update_normalization_grants(
        accepted.job_id, UpdateNormalizationGrants(payload, command.artifacts), "grant", repo
    )
    assert receipt.attempt_token == "next"
    assert ("grants", accepted.job_id, hash_idempotency_key("grant"), payload) in repo.calls
    cancelled = cancel_normalization_job(accepted.job_id, "cancel", repo)
    assert cancelled.status is TradingReviewJobStatus.CANCELLED
    assert ("cancel", accepted.job_id, hash_idempotency_key("cancel")) in repo.calls
    bad = replace(command.artifacts[0], has_credentials_or_fragment=True)
    repo.calls.clear()
    with pytest.raises(NormalizationRequestInvalid, match="Artifact grant URI"):
        update_normalization_grants(
            accepted.job_id, UpdateNormalizationGrants(payload, (bad,)), "key", repo
        )
    assert repo.calls == []


def test_missing_and_noncompleted_jobs_do_not_query_descriptor():
    repo = RecordingJobs()
    assert get_normalization_job_status("missing", repo) is None
    with pytest.raises(NormalizationJobNotFound):
        get_normalization_result("missing", repo)
    accepted = submit_normalization_job(_command(), "key", repo)
    with pytest.raises(NormalizationResultUnavailable):
        get_normalization_result(accepted.job_id, repo)
    assert not any(call[0] == "descriptor" for call in repo.calls)


def test_result_lookup_preserves_completed_then_descriptor_checks():
    repo = RecordingJobs()
    command = _command()
    accepted = submit_normalization_job(command, "key", repo)
    repo.view = replace(repo.view, status=TradingReviewJobStatus.COMPLETED)
    with pytest.raises(NormalizationResultUnavailable):
        get_normalization_result(accepted.job_id, repo)
    now = datetime.now(UTC)
    repo.descriptor = TerminalPayloadDescriptor(
        job_id=accepted.job_id,
        core_logical_job_id="logical-1",
        core_normalization_run_id="run-1",
        payload_id="payload",
        media_type="application/vnd.refinvest.trading-normalization+json",
        byte_size=2,
        payload_sha256="a" * 64,
        input_set_hash=repo.job.computed_input_set_hash,
        hash_specification_version="TRADING_INPUT_SET_HASH_V1",
        target_version_set={},
        reconciliation_manifest_hash="b" * 64,
        result_hash="c" * 64,
        result_schema_version=accepted.result_schema_version,
        result_hash_specification_version=accepted.result_hash_specification_version,
        counts={},
        section_digests=[],
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    assert get_normalization_result(accepted.job_id, repo) is repo.descriptor
    assert [call[0] for call in repo.calls[-2:]] == ["get", "descriptor"]


@pytest.mark.parametrize("query", [get_normalization_job_status, get_normalization_result])
def test_job_identity_error_is_translated_without_a_descriptor_query(query):
    repo = RecordingJobs()
    repo.error = TradingReviewJobStateConflict("Legacy normalization identity is unavailable.")
    with pytest.raises(NormalizationJobIdentityUnavailable) as caught:
        query("job", repo)
    assert caught.value.__cause__ is repo.error
    assert repo.calls == [("get", "job")]
