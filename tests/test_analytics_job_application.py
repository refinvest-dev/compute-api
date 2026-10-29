"""Analytics admission precedence and command/query error boundaries."""

import copy

import pytest

from refinvest.application.analytics_jobs import (
    AnalyticsAdmissionUnavailable,
    AnalyticsCancellationConflict,
    AnalyticsJobCapacity,
    AnalyticsJobConflict,
    AnalyticsJobNotFound,
    AnalyticsJobUnavailable,
    AnalyticsResultExpired,
    AnalyticsResultUnavailable,
    cancel_analytics_job,
    get_analytics_job_status,
    get_analytics_result,
    submit_analytics_job,
    update_analytics_grants,
)
from refinvest.application.trading_review_jobs import hash_idempotency_key
from refinvest.contracts.analytics.identity import AnalyticsContractError, create_fingerprint
from tests.test_trading_analytics_runtime import _request


class RecordingJobs:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.submission = ({"jobId": "job", "status": "PENDING"}, True)
        self.status = {"jobId": "job", "status": "COMPLETED"}
        self.descriptor = {"payloadId": "payload"}
        self.receipt = {"grantRevision": 1}

    def record(self, method, *args):
        self.calls.append((method, *args))
        if method in self.errors:
            raise self.errors[method]

    def create(self, request, key, fingerprint):
        self.record("create", request, key, fingerprint)
        return self.submission

    def get(self, job_id):
        self.record("get", job_id)
        return self.status

    def result_descriptor(self, job_id):
        self.record("descriptor", job_id)
        return self.descriptor

    def cancel(self, job_id, key):
        self.record("cancel", job_id, key)
        return False

    def update_grants(self, job_id, update, key):
        self.record("grants", job_id, update, key)
        return self.receipt


def _submit(request, repository, **policy):
    return submit_analytics_job(
        request,
        "raw-key",
        repository,
        max_input_bytes=policy.get("maximum", 1_000_000),
        implementation_digest=policy.get(
            "digest", request["targetVersionSet"]["implementationDigest"]
        ),
    )


@pytest.mark.parametrize("role", ["ledgerRevisionContent", "reconciliationManifest"])
def test_size_precedes_bad_identity_and_missing_implementation(role):
    request = _request()
    request[role]["byteSize"] = 1_000_001
    request["expectedInputHash"] = "0" * 64
    repo = RecordingJobs()
    with pytest.raises(AnalyticsAdmissionUnavailable):
        _submit(request, repo, digest=None)
    assert repo.calls == []


def test_identity_failure_precedes_missing_implementation():
    request = _request()
    request["expectedInputHash"] = "0" * 64
    repo = RecordingJobs()
    with pytest.raises(AnalyticsContractError, match="INPUT_HASH_MISMATCH"):
        _submit(request, repo, digest=None)
    assert repo.calls == []


@pytest.mark.parametrize("digest", [None, "unsupported-build"])
def test_missing_or_unsupported_build_never_admits(digest):
    repo = RecordingJobs()
    with pytest.raises(AnalyticsAdmissionUnavailable if digest is None else AnalyticsContractError):
        _submit(_request(), repo, digest=digest)
    assert repo.calls == []


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"])
def test_replay_preserves_current_status_and_expired_grants_reach_atomic_store(status):
    request = _request()
    for role in ["ledgerRevisionContent", "reconciliationManifest"]:
        request[role]["expiresAt"] = "2000-01-01T00:00:00Z"
    before = copy.deepcopy(request)
    repo = RecordingJobs()
    repo.submission = ({"jobId": "existing", "status": status}, False)
    assert _submit(request, repo) is repo.submission
    assert request == before
    assert repo.calls == [
        ("create", request, hash_idempotency_key("raw-key"), create_fingerprint(request))
    ]
    assert repo.calls[0][1] is request


@pytest.mark.parametrize(
    "error",
    [
        AnalyticsJobCapacity(),
        AnalyticsJobConflict("IDEMPOTENCY_CONFLICT"),
        AnalyticsJobUnavailable("dependency"),
    ],
)
def test_admission_preserves_adapter_application_errors(error):
    repo = RecordingJobs()
    repo.errors["create"] = error
    with pytest.raises(type(error)) as caught:
        _submit(_request(), repo)
    assert caught.value is error


@pytest.mark.parametrize(
    "status,error", [(None, AnalyticsJobNotFound), ("PENDING", AnalyticsResultUnavailable)]
)
def test_result_does_not_query_descriptor_for_missing_or_noncompleted_job(status, error):
    repo = RecordingJobs()
    repo.status = {"status": status} if status else None
    with pytest.raises(error):
        get_analytics_result("job", repo)
    assert repo.calls == [("get", "job")]


def test_completed_result_missing_and_expired_descriptors_stay_distinct():
    repo = RecordingJobs()
    assert get_analytics_result("job", repo) is repo.descriptor
    assert repo.calls == [("get", "job"), ("descriptor", "job")]
    repo.descriptor = None
    with pytest.raises(AnalyticsResultUnavailable):
        get_analytics_result("job", repo)
    failure = AnalyticsJobUnavailable("any-descriptor-unavailability")
    repo.errors["descriptor"] = failure
    with pytest.raises(AnalyticsResultExpired) as caught:
        get_analytics_result("job", repo)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("operation", [get_analytics_job_status, get_analytics_result])
def test_status_read_errors_are_not_reclassified_as_expired_results(operation):
    repo = RecordingJobs()
    failure = AnalyticsJobUnavailable("read-dependency")
    repo.errors["get"] = failure
    with pytest.raises(AnalyticsJobUnavailable) as caught:
        operation("job", repo)
    assert caught.value is failure
    assert repo.calls == [("get", "job")]


@pytest.mark.parametrize(
    "failure,translated",
    [
        (AnalyticsJobUnavailable(), AnalyticsJobNotFound),
        (AnalyticsJobConflict(), AnalyticsCancellationConflict),
    ],
)
def test_cancellation_maps_only_command_failure_and_does_not_read_after_failure(
    failure, translated
):
    repo = RecordingJobs()
    repo.errors["cancel"] = failure
    with pytest.raises(translated) as caught:
        cancel_analytics_job("job", "cancel-key", repo)
    assert caught.value.__cause__ is failure
    assert repo.calls == [("cancel", "job", hash_idempotency_key("cancel-key"))]


@pytest.mark.parametrize("failure", [AnalyticsJobConflict(), AnalyticsJobUnavailable()])
def test_post_cancel_query_failure_keeps_its_original_error_scope(failure):
    repo = RecordingJobs()
    repo.errors["get"] = failure
    with pytest.raises(type(failure)) as caught:
        cancel_analytics_job("job", "key", repo)
    assert caught.value is failure
    assert [call[0] for call in repo.calls] == ["cancel", "get"]


def test_cancel_returns_the_following_status_and_grants_forward_the_single_projection():
    repo = RecordingJobs()
    assert cancel_analytics_job("job", "cancel-key", repo) is repo.status
    assert repo.calls == [("cancel", "job", hash_idempotency_key("cancel-key")), ("get", "job")]
    update = {"mode": "START_RETRY_ATTEMPT", "nextAttemptToken": "next"}
    assert update_analytics_grants("job", update, "grant-key", repo) is repo.receipt
    assert repo.calls[-1] == ("grants", "job", update, hash_idempotency_key("grant-key"))
    assert repo.calls[-1][2] is update
